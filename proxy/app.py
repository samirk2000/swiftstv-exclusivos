"""HLS proxy API: resolve IP-bound .m3u8 manifests at playback time."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from hls_resolver import (
    CDN_PLAY_REFERER,
    CDN_PLAY_USER_AGENT,
    DEFAULT_PUBLIC_BASE,
    EVENT_UNAVAILABLE_DETAIL,
    CdnFetchError,
    HlsResolver,
    HlsResolverSettings,
    M3U8_CONTENT_TYPE,
    fetch_cdn_with_playout_fallback,
    is_allowed_cdn_url,
    looks_like_playlist,
    rewrite_playlist_absolute,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
LOGGER = logging.getLogger("hls_proxy")

APP = FastAPI(title="Swiftstv HLS Proxy", version="1.0.0")
APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

RESOLVER = HlsResolver(HlsResolverSettings.from_env())
CACHE_TTL_SECONDS = max(0, int(os.environ.get("CACHE_TTL_SECONDS", "45")))
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "").strip()
_CACHE: dict[tuple[str, str, str], tuple[float, list[str]]] = {}
CDN_TOKEN_REJECT_DETAIL = "El CDN rechazó el stream (token/firma). Revisa el proxy HLS."


def proxy_bind_ip() -> str:
    """IP the CDN must see: Render egress, never the phone/Roku."""
    return (getattr(RESOLVER, "egress_ip", None) or "").strip()


@APP.on_event("startup")
async def startup() -> None:
    await RESOLVER.start()
    LOGGER.info("startup mode=transparent egress_ip=%s", proxy_bind_ip())


@APP.on_event("shutdown")
async def shutdown() -> None:
    await RESOLVER.close()


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    cf_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_ip:
        return cf_ip
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def request_public_base(request: Request) -> str:
    proto = (
        request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    ).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    ).split(",")[0].strip()
    host_only = host.split(":")[0].lower()
    if (
        not host
        or host_only in {"localhost", "127.0.0.1", "0.0.0.0"}
        or host_only.endswith(".internal")
    ):
        return DEFAULT_PUBLIC_BASE
    return f"{proto}://{host}"


def require_api_key(request: Request) -> None:
    if not PROXY_API_KEY:
        return
    provided = request.headers.get("x-proxy-key", "").strip()
    if provided != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid proxy API key")


def cache_get(embed: str, referer: str, client_ip: str) -> list[str] | None:
    if CACHE_TTL_SECONDS <= 0:
        return None
    item = _CACHE.get((embed, referer, client_ip))
    if item is None:
        return None
    expires_at, urls = item
    if time.time() > expires_at:
        _CACHE.pop((embed, referer, client_ip), None)
        return None
    return list(urls)


def cache_set(embed: str, referer: str, client_ip: str, urls: list[str]) -> None:
    if CACHE_TTL_SECONDS <= 0 or not urls:
        return
    _CACHE[(embed, referer, client_ip)] = (time.time() + CACHE_TTL_SECONDS, list(urls))


async def resolve_manifests(embed: str, referer: str, client_ip: str) -> list[str]:
    cached = cache_get(embed, referer, client_ip)
    if cached is not None:
        LOGGER.info("cache hit embed=%s ip=%s", embed, client_ip)
        return cached
    try:
        manifests = await RESOLVER.resolve(embed, referer=referer, client_ip=client_ip)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("resolver failed embed=%s ip=%s: %s", embed, client_ip, exc)
        manifests = []
    cache_set(embed, referer, client_ip, manifests)
    return manifests


def raise_cdn_http(exc: CdnFetchError) -> None:
    if exc.status in {401, 403}:
        raise HTTPException(status_code=403, detail=CDN_TOKEN_REJECT_DETAIL) from exc
    if exc.status == 404:
        raise HTTPException(status_code=404, detail=EVENT_UNAVAILABLE_DETAIL) from exc
    raise HTTPException(status_code=502, detail=EVENT_UNAVAILABLE_DETAIL) from exc


@APP.get("/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok",
        "mode": "transparent",
        "egress_ip": proxy_bind_ip(),
    }


@APP.get("/v1/resolve")
async def resolve_json(
    request: Request,
    embed: str = Query(..., min_length=8),
    referer: str = Query(""),
) -> dict[str, Any]:
    require_api_key(request)
    client_ip = get_client_ip(request)
    referer_value = referer or request.headers.get("referer", "") or embed
    manifests = await resolve_manifests(embed, referer_value, client_ip)
    if not manifests:
        raise HTTPException(status_code=404, detail=EVENT_UNAVAILABLE_DETAIL)
    return {
        "client_ip": client_ip,
        "embed": embed,
        "referer": referer_value,
        "m3u8": manifests[0],
        "urls": manifests,
    }


@APP.api_route("/v1/play.m3u8", methods=["GET", "HEAD"])
async def play_manifest(
    request: Request,
    embed: str = Query(..., min_length=8),
    referer: str = Query(""),
) -> Response:
    """Fetch mono.m3u8 on Render and return HTTP 200. Never 302 to the CDN."""
    require_api_key(request)
    viewer_ip = get_client_ip(request)
    bind_ip = proxy_bind_ip()
    referer_value = referer or request.headers.get("referer", "") or embed
    # Do not rewrite ip= to the phone. Leave the token as the player minted it
    # for this proxy's TCP address.
    manifests = await resolve_manifests(embed, referer_value, "")
    if not manifests:
        raise HTTPException(status_code=404, detail=EVENT_UNAVAILABLE_DETAIL)
    try:
        body = await RESOLVER.pipe_playout(
            manifests,
            "",
            public_base=request_public_base(request),
            user_agent=CDN_PLAY_USER_AGENT,
            referer=CDN_PLAY_REFERER,
        )
    except CdnFetchError as exc:
        LOGGER.warning(
            "play pipe failed embed=%s bind_ip=%s viewer=%s: %s",
            embed,
            bind_ip,
            viewer_ip,
            exc,
        )
        raise_cdn_http(exc)
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("play pipe failed embed=%s: %s", embed, exc)
        raise HTTPException(status_code=404, detail=EVENT_UNAVAILABLE_DETAIL) from exc
    LOGGER.info(
        "play 200 (no redirect) bytes=%s bind_ip=%s viewer=%s",
        len(body or ""),
        bind_ip,
        viewer_ip,
    )
    headers = {
        "Cache-Control": "no-store, no-cache, max-age=0",
        "Access-Control-Allow-Origin": "*",
        "Content-Type": M3U8_CONTENT_TYPE,
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=M3U8_CONTENT_TYPE, headers=headers)
    return Response(
        content=body,
        status_code=200,
        media_type=M3U8_CONTENT_TYPE,
        headers=headers,
    )


@APP.api_route("/v1/media", methods=["GET", "HEAD"])
async def proxy_media(
    request: Request,
    u: str = Query(..., min_length=8),
) -> Response:
    """Reverse-proxy a CDN segment/playlist and inject Referer, User-Agent, Host."""
    require_api_key(request)
    if not is_allowed_cdn_url(u):
        LOGGER.warning("blocked media host url=%s", u)
        raise HTTPException(status_code=400, detail="CDN host not allowed")
    client_ip = get_client_ip(request)

    try:
        resolved_url, upstream = await asyncio.to_thread(
            lambda: fetch_cdn_with_playout_fallback(
                u,
                client_ip,
                user_agent=CDN_PLAY_USER_AGENT,
                referer=CDN_PLAY_REFERER,
            )
        )
    except CdnFetchError as exc:
        raise_cdn_http(exc)

    content_type = str(upstream.headers.get("Content-Type") or "application/octet-stream")
    out_headers = {
        "Cache-Control": "no-store, no-cache, max-age=0",
        "Access-Control-Allow-Origin": "*",
    }
    if request.method == "HEAD":
        upstream.close()
        media_type = (
            M3U8_CONTENT_TYPE
            if looks_like_playlist(resolved_url, content_type, b"")
            else content_type
        )
        return Response(status_code=200, media_type=media_type, headers=out_headers)

    peek = upstream.read(16)
    if looks_like_playlist(resolved_url, content_type, peek) or looks_like_playlist(
        upstream.geturl(), content_type, peek
    ):
        try:
            text = (peek + upstream.read()).decode("utf-8", errors="replace")
        finally:
            upstream.close()
        rewritten = rewrite_playlist_absolute(
            text,
            upstream.geturl() or resolved_url,
            public_base=request_public_base(request),
        )
        return Response(content=rewritten, media_type=M3U8_CONTENT_TYPE, headers=out_headers)

    def iterate():
        try:
            if peek:
                yield peek
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(iterate(), media_type=content_type, headers=out_headers)


@APP.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "swiftstv-hls-proxy",
            "endpoints": {
                "health": "/health",
                "resolve": "/v1/resolve?embed=<url>&referer=<url>",
                "play": "/v1/play.m3u8?embed=<url>&referer=<url>",
                "media": "/v1/media?u=<cdn-url>",
            },
            "example_play_url": (
                "/v1/play.m3u8?embed="
                + quote("https://futbollibre.mx/en-vivo/espn-1", safe="")
                + "&referer="
                + quote("https://futbollibre.mx/", safe="")
            ),
        }
    )
