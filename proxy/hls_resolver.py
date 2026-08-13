"""Resolve IP-bound HLS manifests from embed pages using async Playwright."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

LOGGER = logging.getLogger("hls_resolver")
M3U8_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*""", re.IGNORECASE)
PLAYBACK_URL_RE = re.compile(
    r"""(?:playbackURL|source|src|file|hlsUrl)\s*[:=]\s*["'](https?:\\?/\\?/[^\s"']+\.m3u8[^"']*)["']""",
    re.IGNORECASE,
)
SKIP_URL_PREFIXES = ("about:", "javascript:", "blob:", "data:", "chrome-error:")
TUDEPORTESHOY_CDN_SUFFIX = "tudeporteshoy.xyz"
DEAD_PLAYER_HOSTS = ("la12hd.com", "envivo1.com", "streamtp4.com")
AD_HOST_HINTS = (
    "popads",
    "popadscdn",
    "juicyads",
    "googlesyndication",
    "googleadservices",
    "doubleclick.net",
    "googletagservices",
    "googletagmanager",
)
BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font"}
BLOCKED_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".css",
    ".woff2",
    ".gif",
    ".webp",
    ".woff",
    ".ttf",
    ".svg",
)
HLS_CONTENT_HINTS = ("mpegurl", "x-mpegurl", "vnd.apple.mpegurl")
FORCE_PLAY_JS = """() => {
  document.querySelectorAll('video').forEach((v) => {
    try {
      v.muted = true;
      v.autoplay = true;
      v.playsInline = true;
      v.setAttribute('playsinline', '');
      v.setAttribute('autoplay', '');
      const p = v.play();
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch (err) {}
  });
  const cx = Math.floor((window.innerWidth || 800) / 2);
  const cy = Math.floor((window.innerHeight || 600) / 2);
  const hit = document.elementFromPoint(cx, cy);
  if (hit) {
    try { hit.click(); } catch (err) {}
    try {
      hit.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    } catch (err) {}
  }
  document.querySelectorAll(
    'button, .vjs-big-play-button, .jw-icon-display, [class*="play" i], [id*="play" i], [class*="overlay" i], [class*="poster" i]'
  ).forEach((node) => {
    try { node.click(); } catch (err) {}
  });
  if (window.player) {
    try { window.player.configure({ autoPlay: true, mute: true }); } catch (err) {}
    try { window.player.play(); } catch (err) {}
    try { window.player.unmute(); } catch (err) {}
  }
}"""
INIT_AUTOPLAY_JS = """
(() => {
  const forcePlay = () => {
    document.querySelectorAll('video').forEach((v) => {
      try {
        v.muted = true;
        v.autoplay = true;
        v.playsInline = true;
        const p = v.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (err) {}
    });
  };
  const start = () => {
    forcePlay();
    try {
      const observer = new MutationObserver(forcePlay);
      observer.observe(document.documentElement, { childList: true, subtree: true });
    } catch (err) {}
    window.addEventListener('click', forcePlay, true);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
"""
EVENT_UNAVAILABLE_DETAIL = "Evento no disponible o aún no inicia"


def is_tudeporteshoy_embed(url: str) -> bool:
    lower = (url or "").lower()
    return TUDEPORTESHOY_CDN_SUFFIX in lower and "/embed/" in lower


def is_tudeporteshoy_cdn(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith(TUDEPORTESHOY_CDN_SUFFIX) and is_hls_url(url)


def is_dead_player_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == dead or host.endswith(f".{dead}") for dead in DEAD_PLAYER_HOSTS)


def rank_hls_urls(urls: list[str]) -> list[str]:
    def sort_key(url: str) -> tuple[int, str]:
        if is_tudeporteshoy_cdn(url):
            return (0, url)
        return (1, url)

    return sorted(dedupe_keep_order(urls), key=sort_key)


def select_proxy_embed_pages(pages: list[str]) -> list[str]:
    """Prefer live tudeporteshoy embeds; drop dead la12hd iframes when possible."""
    unique = dedupe_keep_order(pages)
    tudeporteshoy = [page for page in unique if "tudeporteshoy.xyz" in page.lower()]
    if tudeporteshoy:
        return tudeporteshoy
    live = [page for page in unique if not is_dead_player_host(page)]
    return live or unique


def parse_host_rewrites(raw: str) -> dict[str, str]:
    """Parse EMBED_HOST_REWRITE like 'la12hd.com=newhost.com,old.com=new.com'."""
    mapping: dict[str, str] = {}
    for part in (raw or "").split(","):
        item = part.strip()
        if not item or "=" not in item:
            continue
        source, target = item.split("=", 1)
        source_host = source.strip().lower()
        target_host = target.strip().lower()
        if source_host and target_host:
            mapping[source_host] = target_host
    return mapping


def rewrite_embed_host(url: str, host_map: dict[str, str]) -> str:
    if not url or not host_map:
        return url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    replacement = host_map.get(host)
    if not replacement:
        return url
    netloc = replacement
    if parsed.port:
        netloc = f"{replacement}:{parsed.port}"
    rewritten = urlunparse(parsed._replace(netloc=netloc))
    LOGGER.info("host rewrite %s -> %s", url, rewritten)
    return rewritten


def unescape_js_url(value: str) -> str:
    return (value or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\/", "/")


def extract_hls_urls(text: str) -> list[str]:
    """Pull .m3u8 URLs out of HTML/JS, including Clappr playbackURL with escaped slashes."""
    cleaned = unescape_js_url(text or "")
    found = list(M3U8_RE.findall(cleaned))
    for match in PLAYBACK_URL_RE.finditer(text or ""):
        found.append(unescape_js_url(match.group(1)))
    for match in PLAYBACK_URL_RE.finditer(cleaned):
        found.append(unescape_js_url(match.group(1)))
    return dedupe_keep_order(found)


def decode_wrapper_player_url(embed_url: str) -> str:
    """Decode tudeporteshoy embed/eventos.html?r=<base64 player url>."""
    parsed = urlparse(embed_url or "")
    path = (parsed.path or "").lower()
    if "embed/eventos.html" not in path and "eventos.html" not in path:
        return ""
    query = parse_qs(parsed.query)
    token = (query.get("r") or query.get("embed") or [""])[0]
    if not token:
        return ""
    padded = token + "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""
    decoded = unescape_js_url(decoded)
    if decoded.startswith("http://") or decoded.startswith("https://"):
        return decoded
    return ""


def is_hls_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower().strip()
    if lower.startswith(SKIP_URL_PREFIXES):
        return False
    return ".m3u8" in lower or "mpegurl" in lower


def is_hls_response(response) -> bool:
    if is_hls_url(getattr(response, "url", "")):
        return True
    headers = getattr(response, "headers", None) or {}
    content_type = str(headers.get("content-type") or "").lower()
    return any(hint in content_type for hint in HLS_CONTENT_HINTS)


def should_block_request(url: str, resource_type: str = "") -> bool:
    if is_hls_url(url):
        return False
    kind = (resource_type or "").lower()
    if kind in {"document", "websocket"}:
        return False
    host = (urlparse(url).hostname or "").lower()
    if any(hint in host for hint in AD_HOST_HINTS):
        return True
    if kind in BLOCKED_RESOURCE_TYPES:
        return True
    path = (urlparse(url).path or "").lower()
    return any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS)


def is_navigable_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower().strip()
    if lower.startswith(SKIP_URL_PREFIXES):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def filter_navigable_urls(urls: list[str]) -> list[str]:
    return [url for url in urls if is_navigable_url(url)]


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
    goto_timeout_ms: int = 8000
    # Upper bound only; resolve returns as soon as the first .m3u8 is sniffed.
    stream_wait_ms: int = 3500
    player_fallback_wait_ms: int = 250
    max_iframe_depth: int = 3
    host_rewrites: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "HlsResolverSettings":
        # Cap dashboard leftovers like HLS_WAIT_MS=12000 so we never sit that long
        # once click-to-play is in place; early-exit still wins on first .m3u8.
        raw_wait = int(os.environ.get("HLS_WAIT_MS", "3500"))
        return cls(
            goto_timeout_ms=int(os.environ.get("HLS_GOTO_TIMEOUT_MS", "8000")),
            stream_wait_ms=max(800, min(raw_wait, 5000)),
            host_rewrites=parse_host_rewrites(os.environ.get("EMBED_HOST_REWRITE", "")),
        )


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
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=(
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-extensions",
                "--autoplay-policy=no-user-gesture-required",
            ),
        )
        self._context = await self._browser.new_context(
            user_agent=self.settings.user_agent,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 720},
            java_script_enabled=True,
        )
        await self._context.add_init_script(INIT_AUTOPLAY_JS)

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
            player_url = decode_wrapper_player_url(embed_url)
            player_url = rewrite_embed_host(player_url, self.settings.host_rewrites)
            if player_url and player_url != embed_url:
                LOGGER.info("decoded wrapper -> %s", player_url[:180])
                captured = await self._resolve_locked(
                    player_url,
                    referer=embed_url or referer,
                    client_ip=client_ip,
                )
                if captured:
                    return captured
                LOGGER.info("player page missed HLS; falling back to wrapper %s", embed_url[:120])
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

        embed_url = rewrite_embed_host(embed_url, self.settings.host_rewrites)
        if not is_navigable_url(embed_url):
            LOGGER.warning("skip non-navigable url depth=%s: %s", depth, embed_url)
            return []

        seen = visited if visited is not None else set()
        if embed_url in seen or depth > self.settings.max_iframe_depth:
            return []
        seen.add(embed_url)

        captured: list[str] = []
        iframe_targets: list[str] = []
        found = asyncio.Event()

        def track(raw_url: str) -> None:
            if not is_hls_url(raw_url):
                return
            rewritten = rewrite_m3u8_client_ip(raw_url, client_ip)
            if rewritten not in captured:
                captured.append(rewritten)
                LOGGER.info("manifest depth=%s %s", depth, rewritten[:180])
            if not found.is_set():
                found.set()

        page = await self._context.new_page()
        page.set_default_timeout(1000)
        page.set_default_navigation_timeout(self.settings.goto_timeout_ms)
        await self._apply_client_ip_routing(page, client_ip, referer)

        def on_request(request) -> None:
            track(request.url)

        async def on_response(response) -> None:
            if is_hls_response(response):
                track(response.url)
            if found.is_set():
                return
            await self._scan_response_body(response, track)

        page.on("request", on_request)
        page.on("response", on_response)

        poke_task: asyncio.Task | None = None
        try:
            # Start click/play loop before navigation finishes so we do not wait
            # on overlays that need a gesture to release the HLS request.
            poke_task = asyncio.create_task(self._poke_player(page, found))
            LOGGER.info("goto depth=%s %s", depth, embed_url)
            try:
                await page.goto(
                    embed_url,
                    wait_until="commit",
                    timeout=self.settings.goto_timeout_ms,
                )
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if "ERR_NAME_NOT_RESOLVED" in message:
                    LOGGER.error(
                        "DNS missing for player host depth=%s %s "
                        "(set EMBED_HOST_REWRITE when a replacement domain appears)",
                        depth,
                        embed_url,
                    )
                elif not found.is_set():
                    LOGGER.warning("goto failed depth=%s %s: %s", depth, embed_url, exc)

            if not found.is_set():
                try:
                    for manifest in extract_hls_urls(await page.content()):
                        track(manifest)
                except Exception:
                    pass

            if not found.is_set():
                await self._kick_player_once(page)
                try:
                    await asyncio.wait_for(
                        found.wait(),
                        timeout=max(0.2, self.settings.stream_wait_ms / 1000),
                    )
                except asyncio.TimeoutError:
                    LOGGER.info(
                        "no manifest within %sms depth=%s",
                        self.settings.stream_wait_ms,
                        depth,
                    )

            # First .m3u8 wins: close immediately and return (no extra waits).
            if captured:
                return rank_hls_urls(captured)

            page_base = page.url if is_navigable_url(page.url) else embed_url
            for frame in page.frames:
                frame_url = rewrite_embed_host(frame.url or "", self.settings.host_rewrites)
                track(frame_url)
                if (
                    is_navigable_url(frame_url)
                    and frame_url != page_base
                    and frame_url != embed_url
                ):
                    iframe_targets.append(frame_url)
                    try:
                        content = await frame.content()
                        for manifest in extract_hls_urls(content):
                            track(manifest)
                    except Exception:
                        continue
            try:
                for iframe in (await page.locator("iframe").all())[:8]:
                    src = (
                        await iframe.get_attribute("src")
                        or await iframe.get_attribute("data-src")
                        or await iframe.get_attribute("data-lazy-src")
                    )
                    if not src or src.startswith(SKIP_URL_PREFIXES):
                        continue
                    absolute = rewrite_embed_host(
                        urljoin(page_base, src), self.settings.host_rewrites
                    )
                    if is_navigable_url(absolute):
                        iframe_targets.append(absolute)
            except Exception:
                pass
        finally:
            if poke_task is not None:
                poke_task.cancel()
                try:
                    await poke_task
                except (asyncio.CancelledError, Exception):
                    pass
            await page.close()

        if captured:
            return rank_hls_urls(captured)

        for iframe_url in filter_navigable_urls(dedupe_keep_order(iframe_targets)):
            if is_dead_player_host(iframe_url):
                LOGGER.info("skip dead player iframe depth=%s %s", depth + 1, iframe_url)
                continue
            LOGGER.info("iframe depth=%s -> %s", depth + 1, iframe_url)
            try:
                captured.extend(
                    await self._resolve_locked(
                        iframe_url,
                        referer=embed_url,
                        client_ip=client_ip,
                        depth=depth + 1,
                        visited=seen,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("iframe resolve failed %s: %s", iframe_url, exc)
            if captured:
                break

        return rank_hls_urls(captured)

    async def _kick_player_once(self, page) -> None:
        """One-shot center click + video.play() (requested gesture for streamtp)."""
        try:
            await page.mouse.click(400, 300)
        except Exception:
            pass
        try:
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            await page.mouse.click(viewport["width"] / 2, viewport["height"] / 2)
        except Exception:
            pass
        try:
            await page.evaluate(FORCE_PLAY_JS)
            await page.evaluate(
                "() => document.querySelectorAll('video').forEach(v => v.play())"
            )
        except Exception:
            pass
        try:
            for frame in page.frames[1:]:
                try:
                    await frame.evaluate(FORCE_PLAY_JS)
                    await frame.evaluate(
                        "() => document.querySelectorAll('video').forEach(v => v.play())"
                    )
                except Exception:
                    continue
        except Exception:
            pass
        try:
            for iframe in (await page.locator("iframe").all())[:8]:
                box = await iframe.bounding_box()
                if not box:
                    continue
                await page.mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                try:
                    await iframe.click(timeout=300, force=True)
                except Exception:
                    pass
        except Exception:
            pass

    async def _poke_player(self, page, found: asyncio.Event) -> None:
        """Click overlays and force video.play() until an HLS request appears."""
        while not found.is_set():
            await self._kick_player_once(page)
            try:
                await asyncio.wait_for(found.wait(), timeout=0.2)
                return
            except asyncio.TimeoutError:
                continue

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
            url = request.url
            if not is_navigable_url(url):
                await route.abort()
                return
            if should_block_request(url, getattr(request, "resource_type", "") or ""):
                await route.abort()
                return
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
            if resource_type == "media" or any(hint in content_type for hint in HLS_CONTENT_HINTS):
                track(response.url)
            if resource_type not in {"xhr", "fetch", "script", "document", "media"}:
                return
            if not any(token in content_type for token in ("json", "javascript", "text", "mpegurl")):
                return
            body = await response.text()
            if not body or len(body) > 500_000:
                return
            for manifest in extract_hls_urls(body):
                track(manifest)
        except Exception:
            return
