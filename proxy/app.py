"""HLS proxy API: resolve IP-bound .m3u8 manifests at playback time."""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from hls_resolver import HlsResolver, HlsResolverSettings

APP = FastAPI(title="Swiftstv HLS Proxy", version="1.0.0")
APP.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

RESOLVER = HlsResolver(
    HlsResolverSettings(
        stream_wait_ms=int(os.environ.get("HLS_WAIT_MS", "12000")),
        goto_timeout_ms=int(os.environ.get("HLS_GOTO_TIMEOUT_MS", "15000")),
    )
)
CACHE_TTL_SECONDS = max(0, int(os.environ.get("CACHE_TTL_SECONDS", "45")))
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "").strip()
_CACHE: dict[tuple[str, str, str], tuple[float, list[str]]] = {}


@APP.on_event("startup")
async def startup() -> None:
    await RESOLVER.start()


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
        return cached
    manifests = await RESOLVER.resolve(embed, referer=referer, client_ip=client_ip)
    cache_set(embed, referer, client_ip, manifests)
    return manifests


@APP.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
        raise HTTPException(status_code=404, detail="No .m3u8 manifest captured for this embed")
    return {
        "client_ip": client_ip,
        "embed": embed,
        "referer": referer_value,
        "m3u8": manifests[0],
        "urls": manifests,
    }


@APP.get("/v1/play.m3u8")
async def play_redirect(
    request: Request,
    embed: str = Query(..., min_length=8),
    referer: str = Query(""),
) -> Response:
    """Return a redirect to the resolved manifest for HLS players (Roku, etc.)."""
    require_api_key(request)
    client_ip = get_client_ip(request)
    referer_value = referer or request.headers.get("referer", "") or embed
    manifests = await resolve_manifests(embed, referer_value, client_ip)
    if not manifests:
        raise HTTPException(status_code=404, detail="No .m3u8 manifest captured for this embed")
    return RedirectResponse(url=manifests[0], status_code=302)


@APP.get("/")
async def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "swiftstv-hls-proxy",
            "endpoints": {
                "health": "/health",
                "resolve": "/v1/resolve?embed=<url>&referer=<url>",
                "play": "/v1/play.m3u8?embed=<url>&referer=<url>",
            },
            "example_play_url": (
                "/v1/play.m3u8?embed="
                + quote("https://futbollibre.mx/en-vivo/espn-1", safe="")
                + "&referer="
                + quote("https://futbollibre.mx/", safe="")
            ),
        }
    )
