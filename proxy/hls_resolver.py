"""Resolve IP-bound HLS manifests from embed pages using async Playwright."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

M3U8_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*""", re.IGNORECASE)


def is_hls_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower().strip()
    if lower.startswith(("blob:", "data:", "about:")):
        return False
    return ".m3u8" in lower


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def rewrite_m3u8_client_ip(m3u8_url: str, client_ip: str) -> str:
    """Replace ip= query param when the CDN accepts client-supplied IP tokens."""
    parsed = urlparse(m3u8_url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.lower() == "ip" for key, _ in params):
        return m3u8_url
    updated = [(key, client_ip if key.lower() == "ip" else value) for key, value in params]
    return urlunparse(parsed._replace(query=urlencode(updated)))


@dataclass
class HlsResolverSettings:
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    goto_timeout_ms: int = 15000
    stream_wait_ms: int = 12000
    player_fallback_wait_ms: int = 3000
    max_iframe_depth: int = 3


class HlsResolver:
    """Capture .m3u8 URLs while simulating the viewer IP on every network hop."""

    def __init__(self, settings: HlsResolverSettings | None = None) -> None:
        self.settings = settings or HlsResolverSettings()
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self.settings.user_agent,
            ignore_https_errors=True,
        )

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def resolve(self, embed_url: str, referer: str, client_ip: str) -> list[str]:
        async with self._lock:
            return await self._resolve_locked(embed_url, referer, client_ip)

    async def _resolve_locked(
        self,
        embed_url: str,
        referer: str,
        client_ip: str,
        *,
        depth: int = 0,
        visited: set[str] | None = None,
    ) -> list[str]:
        if self._context is None:
            raise RuntimeError("HlsResolver.start() must be called before resolve()")

        seen = visited if visited is not None else set()
        if embed_url in seen or depth > self.settings.max_iframe_depth:
            return []
        seen.add(embed_url)

        captured: list[str] = []
        iframe_targets: list[str] = []

        def track(raw_url: str) -> None:
            if not is_hls_url(raw_url):
                return
            rewritten = rewrite_m3u8_client_ip(raw_url, client_ip)
            if rewritten not in captured:
                captured.append(rewritten)

        page = await self._context.new_page()
        await self._apply_client_ip_routing(page, client_ip, referer)

        async def on_response(response) -> None:
            track(response.url)
            await self._scan_response_body(response, track)

        page.on("response", on_response)

        try:
            await page.goto(
                embed_url,
                wait_until="domcontentloaded",
                timeout=self.settings.goto_timeout_ms,
            )
            try:
                await page.wait_for_selector("iframe, video, source", timeout=8000)
            except Exception:
                pass
            try:
                await page.wait_for_response(
                    lambda response: is_hls_url(response.url),
                    timeout=self.settings.stream_wait_ms,
                )
            except Exception:
                await page.wait_for_timeout(self.settings.player_fallback_wait_ms)

            for frame in page.frames:
                frame_url = frame.url or ""
                track(frame_url)
                if frame_url and frame_url not in {"about:blank"} and frame_url != page.url:
                    iframe_targets.append(frame_url)
                try:
                    content = await frame.content()
                    for manifest in M3U8_RE.findall(content):
                        track(manifest)
                except Exception:
                    continue

            for iframe in (await page.locator("iframe").all())[:8]:
                src = (
                    await iframe.get_attribute("src")
                    or await iframe.get_attribute("data-src")
                    or await iframe.get_attribute("data-lazy-src")
                )
                if not src or src.startswith(("about:", "javascript:")):
                    continue
                iframe_targets.append(urljoin(page.url, src))
        finally:
            await page.close()

        if not captured:
            for iframe_url in dedupe_keep_order(iframe_targets):
                captured.extend(
                    await self._resolve_locked(
                        iframe_url,
                        referer=embed_url,
                        client_ip=client_ip,
                        depth=depth + 1,
                        visited=seen,
                    )
                )

        return dedupe_keep_order(captured)

    async def _apply_client_ip_routing(self, page, client_ip: str, referer: str) -> None:
        headers = {
            "Referer": referer,
            "X-Forwarded-For": client_ip,
            "X-Real-IP": client_ip,
            "CF-Connecting-IP": client_ip,
            "True-Client-IP": client_ip,
        }
        await page.set_extra_http_headers(headers)

        async def handle_route(route, request) -> None:
            merged = dict(request.headers)
            merged.update(headers)
            await route.continue_(headers=merged)

        await page.route("**/*", handle_route)

    @staticmethod
    async def _scan_response_body(response, track: Callable[[str], None]) -> None:
        try:
            if not response.ok:
                return
            resource_type = getattr(response.request, "resource_type", "")
            content_type = (response.headers.get("content-type") or "").lower()
            if resource_type == "media" or "mpegurl" in content_type:
                track(response.url)
            if resource_type not in {"xhr", "fetch", "script", "document", "media"}:
                return
            if not any(token in content_type for token in ("json", "javascript", "text", "mpegurl")):
                return
            body = await response.text()
            if not body or len(body) > 500_000:
                return
            for manifest in M3U8_RE.findall(body):
                track(manifest)
        except Exception:
            return
