"""Resolve IP-bound HLS manifests from embed pages using async Playwright."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

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
    if not url:
        return url
    rewritten = url
    if host_map:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        replacement = host_map.get(host)
        if replacement:
            netloc = replacement
            if parsed.port:
                netloc = f"{replacement}:{parsed.port}"
            rewritten = urlunparse(parsed._replace(netloc=netloc))
            LOGGER.info("host rewrite %s -> %s", url, rewritten)
    return rewrite_player_script(rewritten)


def rewrite_player_script(url: str) -> str:
    """global1.php is IP-blocked; the live player is global2.php."""
    if "/global1.php" not in (url or ""):
        return url
    rewritten = url.replace("/global1.php", "/global2.php")
    LOGGER.info("player script rewrite global1.php -> global2.php")
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
        return rewrite_player_script(decoded)
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


def replace_raw_query_value(query: str, name: str, new_value: str) -> str:
    """Replace one query param without re-encoding or reordering the rest.

    parse_qsl + urlencode turns '+' into space and '=' into %3D, which breaks
    CDN token/hash signatures.
    """
    if not query or not name:
        return query
    parts: list[str] = []
    found = False
    for part in query.split("&"):
        key, sep, _old = part.partition("=")
        if sep and key.lower() == name.lower():
            parts.append(f"{key}={new_value}")
            found = True
        else:
            parts.append(part)
    if not found:
        return query
    return "&".join(parts)


def raw_query_value(url: str, name: str) -> str:
    """Read one query param without parse_qsl (keeps '+' and '=' bytes)."""
    query = urlparse(url or "").query
    if not query or not name:
        return ""
    for part in query.split("&"):
        key, sep, value = part.partition("=")
        if sep and key.lower() == name.lower():
            return value
    return ""


UNIX_TS_MIN = 1_700_000_000
UNIX_TS_MAX = 2_100_000_000


def token_expiry_unix(token: str) -> int:
    """Best-effort expiry from tokens like hash-id-<exp>-<iat>."""
    stamps: list[int] = []
    for part in (token or "").split("-"):
        if not part.isdigit():
            continue
        value = int(part)
        if UNIX_TS_MIN <= value <= UNIX_TS_MAX:
            stamps.append(value)
    return min(stamps) if stamps else 0


def manifest_urls_expire_at(urls: list[str], ttl_seconds: float) -> float:
    """Cache until TOKEN_TTL, or sooner if the CDN token timestamp is earlier."""
    now = time.time()
    expires_at = now + max(0.0, float(ttl_seconds))
    stamps = [token_expiry_unix(raw_query_value(url, "token")) for url in urls]
    stamps = [stamp for stamp in stamps if stamp]
    if stamps:
        expires_at = min(expires_at, float(min(stamps) - 90))
    return max(now + 5.0, expires_at)


def rewrite_m3u8_client_ip(m3u8_url: str, client_ip: str) -> str:
    """Replace ip= in-place. Leave token/auth/hash bytes untouched."""
    if not client_ip:
        return m3u8_url
    parsed = urlparse(m3u8_url)
    if not parsed.query:
        return m3u8_url
    new_query = replace_raw_query_value(parsed.query, "ip", client_ip)
    if new_query == parsed.query:
        return m3u8_url
    return urlunparse(parsed._replace(query=new_query))


def is_media_playlist_url(url: str) -> bool:
    lower = (url or "").lower()
    return "mono.m3u8" in lower or "tracks-v1" in lower


def is_master_index_url(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return path.endswith("/index.m3u8") or path.endswith("index.m3u8")


def to_po_playout_url(url: str) -> str:
    """Map Clappr master URLs to the Roku-playable media playlist.

    https://si.tudeporteshoy.xyz:443/global/sportv_1pt/index.m3u8?token=...&ip=...
    -> https://po.tudeporteshoy.xyz/sportv_1pt/tracks-v1a1/mono.m3u8?ip=...&token=...
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not host.endswith("tudeporteshoy.xyz"):
        return ""
    path = parsed.path or ""
    media_path = ""
    match = re.match(r"/global/([^/]+)/index\.m3u8$", path, re.IGNORECASE)
    if match:
        media_path = f"/{match.group(1)}/tracks-v1a1/mono.m3u8"
    else:
        match = re.match(r"/global/([^/]+)/(.+\.m3u8)$", path, re.IGNORECASE)
        if match:
            media_path = f"/{match.group(1)}/{match.group(2)}"
        elif is_media_playlist_url(url) and not host.startswith("po."):
            media_path = path
        elif host.startswith("po.") and is_media_playlist_url(url):
            media_path = path
        else:
            return ""
    # Keep the original query string (token/auth/hash order and encoding).
    return urlunparse(("https", "po.tudeporteshoy.xyz", media_path, "", parsed.query, ""))


def playout_fallback_url(url: str) -> str:
    """Map a blocked edge/master URL to po.tudeporteshoy.xyz, query unchanged."""
    mapped = to_po_playout_url(url or "")
    if not mapped or mapped == url:
        return ""
    return mapped


def should_fallback_to_playout(url: str, status: int) -> bool:
    if status not in {401, 403, 404}:
        return False
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("po."):
        return False
    return bool(playout_fallback_url(url))


def expand_playout_candidates(urls: list[str]) -> list[str]:
    """Prefer po. playout; keep the original master as a later candidate."""
    expanded: list[str] = []
    for url in urls:
        if not url:
            continue
        fallback = playout_fallback_url(url)
        if fallback:
            expanded.append(fallback)
        expanded.append(url)
    return rank_hls_urls(expanded)


def rank_hls_urls(urls: list[str]) -> list[str]:
    def sort_key(url: str) -> tuple[int, int, int, str]:
        host = (urlparse(url).hostname or "").lower()
        media = 0 if is_media_playlist_url(url) else 1
        po_host = 0 if host.startswith("po.") else 1
        master = 1 if is_master_index_url(url) else 0
        return (media, po_host, master, url)

    return sorted(dedupe_keep_order(urls), key=sort_key)


CDN_PLAY_REFERER = "https://tudeporteshoy.xyz/"
CDN_PLAY_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_PUBLIC_BASE = os.environ.get(
    "PROXY_PUBLIC_BASE", "https://swiftstv-hls-proxy.onrender.com"
)
M3U8_CONTENT_TYPE = "application/vnd.apple.mpegurl"
LEAKED_CDN_URL_RE = re.compile(
    r"https?://[^\s\"']*tudeporteshoy\.xyz[^\s\"']*",
    re.IGNORECASE,
)
URI_ATTR_RE = re.compile(
    r'(URI=)(?P<quote>["\'])(?P<uri>.*?)(?P=quote)',
    re.IGNORECASE,
)


def detect_egress_ip() -> str:
    """Public IP Render uses when it talks to the CDN."""
    from urllib.error import URLError
    from urllib.request import Request, urlopen

    for endpoint in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            request = Request(endpoint, headers={"User-Agent": "curl/8.0"})
            with urlopen(request, timeout=6) as response:
                ip = response.read().decode("utf-8", errors="ignore").strip()
            if ip and " " not in ip and len(ip) < 64:
                return ip
        except (URLError, OSError, TimeoutError):
            continue
    return ""


def cdn_fetch_headers(
    url: str,
    client_ip: str,
    *,
    user_agent: str = "",
    referer: str = "",
) -> dict[str, str]:
    """Headers the origin player sends. Do not spoof the phone IP.

    The CDN signs ip=/token= to the TCP peer. Render must fetch as itself.
    """
    parsed = urlparse(url)
    return {
        "User-Agent": CDN_PLAY_USER_AGENT,
        "Referer": CDN_PLAY_REFERER,
        "Origin": "https://tudeporteshoy.xyz",
        "Host": parsed.netloc,
        "Accept": "*/*",
    }


def allowed_cdn_host(host: str) -> bool:
    hostname = (host or "").lower()
    if not hostname or hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return False
    suffixes = ["tudeporteshoy.xyz"]
    suffixes.extend(
        part.strip().lower()
        for part in os.environ.get("CDN_HOST_ALLOW", "").split(",")
        if part.strip()
    )
    return any(hostname == suffix or hostname.endswith(f".{suffix}") for suffix in suffixes)


def is_allowed_cdn_url(url: str) -> bool:
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    return allowed_cdn_host(parsed.hostname or "")


def wrap_media_url(public_base: str, cdn_url: str) -> str:
    """Point the player at /v1/media so it never talks to po.tudeporteshoy.xyz."""
    base = (public_base or DEFAULT_PUBLIC_BASE).rstrip("/")
    if not cdn_url or not is_navigable_url(cdn_url):
        return cdn_url
    if "/v1/media" in cdn_url:
        return cdn_url
    return f"{base}/v1/media?u={quote(cdn_url, safe='')}"


def absolutize_hls_uri(uri: str, base_url: str) -> str:
    """Turn a playlist URI into an absolute URL without touching existing query bytes."""
    cleaned = unescape_js_url((uri or "").strip())
    if not cleaned or cleaned.startswith(("data:", "#", "urn:")):
        return cleaned
    absolute = urljoin(base_url, cleaned)
    parsed_abs = urlparse(absolute)
    if parsed_abs.scheme not in {"http", "https"}:
        return absolute
    parsed_uri = urlparse(cleaned)
    if parsed_uri.query or parsed_abs.query:
        return absolute
    base_query = urlparse(base_url).query
    if not base_query:
        return absolute
    if parsed_uri.scheme in {"http", "https"}:
        base_host = (urlparse(base_url).hostname or "").lower()
        if (parsed_abs.hostname or "").lower() != base_host:
            return absolute
    return urlunparse(parsed_abs._replace(query=base_query))


def rewrite_playlist_absolute(body: str, base_url: str, public_base: str = "") -> str:
    """Rewrite .ts / sub-playlist / KEY URIs to /v1/media?u=... (never leak CDN URLs)."""
    proxy_base = public_base or DEFAULT_PUBLIC_BASE

    def convert(uri: str) -> str:
        return wrap_media_url(proxy_base, absolutize_hls_uri(uri, base_url))

    def replace_uri(match: re.Match[str]) -> str:
        return (
            f"{match.group(1)}{match.group('quote')}"
            f"{convert(match.group('uri'))}"
            f"{match.group('quote')}"
        )

    lines: list[str] = []
    for raw in (body or "").splitlines():
        line = raw.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped.startswith("#"):
            lines.append(URI_ATTR_RE.sub(replace_uri, stripped))
            continue
        lines.append(convert(stripped))
    rewritten = "\n".join(lines)
    if (body or "").endswith("\n"):
        rewritten += "\n"
    return _wrap_leaked_cdn_urls(rewritten, proxy_base)


def _wrap_leaked_cdn_urls(body: str, public_base: str) -> str:
    """Safety net: any leftover https://*.tudeporteshoy.xyz URL goes through /v1/media."""

    def repl(match: re.Match[str]) -> str:
        url = match.group(0)
        if "/v1/media" in url:
            return url
        return wrap_media_url(public_base, url)

    return LEAKED_CDN_URL_RE.sub(repl, body or "")


def first_variant_uri(body: str, base_url: str) -> str:
    """Return the first media playlist URI from a master playlist."""
    pending = False
    for raw in (body or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith("#EXT-X-STREAM-INF"):
            attr = URI_ATTR_RE.search(stripped)
            if attr:
                return absolutize_hls_uri(attr.group("uri"), base_url)
            pending = True
            continue
        if pending and stripped and not stripped.startswith("#"):
            return absolutize_hls_uri(stripped, base_url)
        if stripped:
            pending = False
    return ""


def is_master_playlist_body(body: str) -> bool:
    return "#EXT-X-STREAM-INF" in (body or "").upper()


def looks_like_playlist(url: str, content_type: str, body: bytes) -> bool:
    lower_url = (url or "").lower()
    if ".m3u8" in lower_url or "mpegurl" in (content_type or "").lower():
        return True
    head = body[:16].lstrip().upper()
    return head.startswith(b"#EXTM3U")


def log_cdn_request(url: str, headers: dict[str, str], client_ip: str) -> None:
    LOGGER.info(
        "CDN request GET %s | Host=%s Referer=%s User-Agent=%s X-Forwarded-For=%s",
        url,
        headers.get("Host", ""),
        headers.get("Referer", ""),
        headers.get("User-Agent", ""),
        client_ip,
    )


def log_cdn_response(
    url: str,
    status: int,
    response_headers=None,
    body_preview: bytes = b"",
) -> None:
    content_type = ""
    if response_headers is not None:
        content_type = str(
            response_headers.get("Content-Type")
            or response_headers.get("content-type")
            or ""
        )
    preview = body_preview[:400].decode("utf-8", errors="replace").replace("\n", "\\n")
    if status in {401, 403}:
        LOGGER.warning(
            "CDN rejected stream (token/signature) status=%s url=%s content-type=%s body=%s",
            status,
            url,
            content_type,
            preview,
        )
        return
    if status >= 400:
        LOGGER.warning(
            "CDN error status=%s url=%s content-type=%s body=%s",
            status,
            url,
            content_type,
            preview,
        )
        return
    LOGGER.info("CDN response status=%s url=%s content-type=%s", status, url, content_type)


class CdnFetchError(RuntimeError):
    def __init__(self, status: int, url: str, message: str) -> None:
        super().__init__(f"HTTP {status}: {message} url={url}")
        self.status = status
        self.url = url


def fetch_cdn(
    url: str,
    client_ip: str,
    *,
    timeout: int = 15,
    user_agent: str = "",
    referer: str = "",
):
    """GET from the CDN with Referer/UA. Follow 302 on the server; never expose it."""
    from urllib.error import HTTPError, URLError
    from urllib.request import HTTPRedirectHandler, Request, build_opener

    headers = cdn_fetch_headers(
        url, client_ip, user_agent=user_agent, referer=referer
    )
    log_cdn_request(url, headers, client_ip)

    class ProxyRedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            LOGGER.info(
                "CDN HTTP %s followed on proxy (not forwarded to app) %s -> %s",
                code,
                req.full_url,
                newurl,
            )
            hop_ip = req.get_header("X-forwarded-for") or client_ip
            hop_headers = cdn_fetch_headers(newurl, hop_ip)
            return Request(newurl, headers=hop_headers, method="GET")

    opener = build_opener(ProxyRedirectHandler)
    request = Request(url, headers=headers)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        fail_url = str(getattr(exc, "url", None) or url)
        try:
            err_body = exc.read() or b""
        except Exception:
            err_body = b""
        log_cdn_response(fail_url, exc.code, exc.headers, err_body)
        raise CdnFetchError(exc.code, fail_url, exc.reason) from exc
    except URLError as exc:
        LOGGER.warning("CDN transport error url=%s error=%s", url, exc.reason or exc)
        raise CdnFetchError(0, url, str(exc.reason or exc)) from exc
    status = getattr(response, "status", None) or response.getcode()
    log_cdn_response(response.geturl() or url, int(status), response.headers)
    return response


def fetch_cdn_with_playout_fallback(
    url: str,
    client_ip: str,
    *,
    timeout: int = 15,
    user_agent: str = "",
    referer: str = "",
):
    """GET a CDN URL; on master 401/403/404 retry po.tudeporteshoy.xyz."""
    try:
        return url, fetch_cdn(
            url,
            client_ip,
            timeout=timeout,
            user_agent=user_agent,
            referer=referer,
        )
    except CdnFetchError as exc:
        fallback = playout_fallback_url(url)
        if not should_fallback_to_playout(url, exc.status) or not fallback:
            raise
        LOGGER.warning(
            "master playlist HTTP %s (CDN IP/token block) url=%s; fallback playout %s",
            exc.status,
            url,
            fallback,
        )
        return fallback, fetch_cdn(
            fallback,
            client_ip,
            timeout=timeout,
            user_agent=user_agent,
            referer=referer,
        )


def fetch_m3u8_text(
    url: str,
    client_ip: str,
    timeout: int = 12,
    *,
    user_agent: str = "",
    referer: str = "",
) -> str:
    response = fetch_cdn(
        url,
        client_ip,
        timeout=timeout,
        user_agent=user_agent,
        referer=referer,
    )
    try:
        return response.read().decode("utf-8", errors="replace")
    finally:
        response.close()


async def pipe_playlist(
    urls: list[str],
    client_ip: str,
    playwright_request=None,
    *,
    public_base: str = "",
    user_agent: str = "",
    referer: str = "",
) -> str:
    """Fetch the first working playlist and return it with proxied segment URLs."""
    last_error = "no manifests"
    last_fetch_error: CdnFetchError | None = None
    for url in expand_playout_candidates(urls):
        if not url:
            continue
        try:
            source_url, body = await _load_playlist_or_playout(
                url,
                client_ip,
                playwright_request,
                user_agent=user_agent,
                referer=referer,
            )
        except CdnFetchError as exc:
            last_error = str(exc)
            last_fetch_error = exc
            LOGGER.info("playlist pipe failed %s", last_error)
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            LOGGER.info("playlist pipe failed %s", last_error)
            continue
        if not body or "#EXTM3U" not in body.upper():
            last_error = f"not an HLS playlist url={source_url}"
            LOGGER.warning("%s", last_error)
            continue
        if is_master_playlist_body(body):
            po = playout_fallback_url(source_url) or to_po_playout_url(source_url)
            variant = first_variant_uri(body, source_url)
            for next_url in dedupe_keep_order([po, variant]):
                if not next_url:
                    continue
                try:
                    source_url, body = await _load_playlist_or_playout(
                        next_url,
                        client_ip,
                        playwright_request,
                        user_agent=user_agent,
                        referer=referer,
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    LOGGER.info("variant/playout pipe failed %s", exc)
        rewritten = rewrite_playlist_absolute(body, source_url, public_base=public_base)
        LOGGER.info("piped playlist %s (%s bytes)", source_url, len(rewritten))
        return rewritten
    if last_fetch_error is not None:
        raise last_fetch_error
    raise RuntimeError(last_error)


async def _load_playlist_or_playout(
    url: str,
    client_ip: str,
    playwright_request=None,
    *,
    user_agent: str = "",
    referer: str = "",
) -> tuple[str, str]:
    try:
        body = await _fetch_playlist_body(
            url,
            client_ip,
            playwright_request,
            user_agent=user_agent,
            referer=referer,
        )
        return url, body
    except CdnFetchError as exc:
        fallback = playout_fallback_url(url)
        if not should_fallback_to_playout(url, exc.status) or not fallback:
            raise
        LOGGER.warning(
            "master playlist HTTP %s (CDN IP/token block) url=%s; fallback playout %s",
            exc.status,
            url,
            fallback,
        )
        body = await _fetch_playlist_body(
            fallback,
            client_ip,
            playwright_request,
            user_agent=user_agent,
            referer=referer,
        )
        return fallback, body


async def _fetch_playlist_body(
    url: str,
    client_ip: str,
    playwright_request=None,
    *,
    user_agent: str = "",
    referer: str = "",
) -> str:
    try:
        return await asyncio.to_thread(
            lambda: fetch_m3u8_text(
                url,
                client_ip,
                user_agent=user_agent,
                referer=referer,
            )
        )
    except Exception as urllib_exc:
        if playwright_request is None:
            raise
        LOGGER.info("urllib playlist miss, retry via playwright %s", urllib_exc)
        headers = cdn_fetch_headers(
            url, client_ip, user_agent=user_agent, referer=referer
        )
        log_cdn_request(url, headers, client_ip)
        response = await playwright_request.get(url, headers=headers, timeout=12000)
        body = await response.text()
        log_cdn_response(
            url,
            int(response.status),
            {"Content-Type": response.headers.get("content-type", "")},
            body.encode("utf-8", errors="replace")[:400],
        )
        if not response.ok:
            raise CdnFetchError(int(response.status), url, "playwright") from urllib_exc
        return body


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
    token_ttl_seconds: int = 1800

    @classmethod
    def from_env(cls) -> "HlsResolverSettings":
        # Cap dashboard leftovers like HLS_WAIT_MS=12000 so we never sit that long
        # once click-to-play is in place; early-exit still wins on first .m3u8.
        raw_wait = int(os.environ.get("HLS_WAIT_MS", "3500"))
        token_ttl = os.environ.get("TOKEN_TTL_SECONDS")
        if token_ttl is None:
            token_ttl = os.environ.get("CACHE_TTL_SECONDS", "1800")
        return cls(
            goto_timeout_ms=int(os.environ.get("HLS_GOTO_TIMEOUT_MS", "8000")),
            stream_wait_ms=max(800, min(raw_wait, 5000)),
            host_rewrites=parse_host_rewrites(os.environ.get("EMBED_HOST_REWRITE", "")),
            token_ttl_seconds=max(0, int(token_ttl)),
        )


class HlsResolver:
    """Capture .m3u8 URLs. Tokens are minted for this proxy's egress IP."""

    def __init__(self, settings: HlsResolverSettings | None = None) -> None:
        self.settings = settings or HlsResolverSettings()
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()
        self.egress_ip = ""
        self._token_cache: dict[str, tuple[float, list[str]]] = {}
        self.token_hits = 0
        self.token_misses = 0

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self.egress_ip = await asyncio.to_thread(detect_egress_ip)
        LOGGER.info("proxy egress ip=%s (CDN tokens bind to this, not the phone)", self.egress_ip)

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

    def token_cache_key(self, embed_url: str) -> str:
        player_url = decode_wrapper_player_url(embed_url)
        player_url = rewrite_embed_host(
            player_url or embed_url, self.settings.host_rewrites
        )
        return player_url or embed_url

    def token_cache_stats(self) -> dict[str, int]:
        now = time.time()
        live = sum(1 for exp, _urls in self._token_cache.values() if exp > now)
        return {
            "entries": live,
            "hits": self.token_hits,
            "misses": self.token_misses,
            "ttl_seconds": self.settings.token_ttl_seconds,
        }

    def _token_cache_get(self, key: str) -> list[str] | None:
        if self.settings.token_ttl_seconds <= 0:
            return None
        item = self._token_cache.get(key)
        if item is None:
            return None
        expires_at, urls = item
        if time.time() >= expires_at or not urls:
            self._token_cache.pop(key, None)
            return None
        self.token_hits += 1
        LOGGER.info(
            "token cache hit key=%s ttl_left=%ss",
            key[:120],
            max(0, int(expires_at - time.time())),
        )
        return list(urls)

    def _token_cache_set(self, key: str, urls: list[str]) -> None:
        if self.settings.token_ttl_seconds <= 0 or not urls:
            return
        expires_at = manifest_urls_expire_at(urls, self.settings.token_ttl_seconds)
        self._token_cache[key] = (expires_at, list(urls))
        LOGGER.info(
            "token cache store key=%s ttl=%ss url=%s",
            key[:120],
            max(0, int(expires_at - time.time())),
            urls[0][:160],
        )

    def invalidate_tokens(self, *, embed: str = "", failed_url: str = "") -> int:
        """Drop cached Playwright captures when the CDN rejects the token."""
        token = raw_query_value(failed_url, "token")
        embed_key = self.token_cache_key(embed) if embed else ""
        removed = 0
        for key, (_exp, urls) in list(self._token_cache.items()):
            if embed_key and key == embed_key:
                self._token_cache.pop(key, None)
                removed += 1
                continue
            if token and any(token in url for url in urls):
                self._token_cache.pop(key, None)
                removed += 1
        if not embed_key and not token and self._token_cache:
            removed = len(self._token_cache)
            self._token_cache.clear()
        if removed:
            LOGGER.info(
                "token cache invalidate removed=%s embed=%s",
                removed,
                (embed or failed_url)[:120],
            )
        return removed

    async def resolve(
        self,
        embed_url: str,
        referer: str,
        client_ip: str,
        *,
        force: bool = False,
    ) -> list[str]:
        cache_key = self.token_cache_key(embed_url)
        if not force:
            cached = self._token_cache_get(cache_key)
            if cached is not None:
                return cached
        async with self._lock:
            if not force:
                cached = self._token_cache_get(cache_key)
                if cached is not None:
                    return cached
            self.token_misses += 1
            LOGGER.info("token cache miss; opening player key=%s", cache_key[:120])
            player_url = decode_wrapper_player_url(embed_url)
            player_url = rewrite_embed_host(player_url, self.settings.host_rewrites)
            captured: list[str] = []
            if player_url and player_url != embed_url:
                LOGGER.info("decoded wrapper -> %s", player_url[:180])
                captured = await self._resolve_locked(
                    player_url,
                    referer=embed_url or referer,
                    client_ip=client_ip,
                )
                if not captured:
                    LOGGER.info(
                        "player page missed HLS; falling back to wrapper %s",
                        embed_url[:120],
                    )
            if not captured:
                captured = await self._resolve_locked(embed_url, referer, client_ip)
            ranked = self._finalize_manifests(captured, client_ip)
            if ranked:
                self._token_cache_set(cache_key, ranked)
            return ranked

    async def pipe_playout(
        self,
        urls: list[str],
        client_ip: str,
        *,
        public_base: str = "",
        user_agent: str = "",
        referer: str = "",
    ) -> str:
        request = getattr(self._context, "request", None) if self._context else None
        return await pipe_playlist(
            urls,
            client_ip,
            request,
            public_base=public_base,
            user_agent=user_agent,
            referer=referer,
        )

    @staticmethod
    def _finalize_manifests(urls: list[str], client_ip: str) -> list[str]:
        """Map Clappr index.m3u8 to po./mono.m3u8 for the playlist pipe."""
        promoted: list[str] = []
        for url in urls:
            rewritten = rewrite_m3u8_client_ip(url, client_ip)
            playout = to_po_playout_url(rewritten)
            if playout:
                promoted.append(rewrite_m3u8_client_ip(playout, client_ip))
                LOGGER.info("playout %s", playout[:180])
            if is_media_playlist_url(rewritten) or not is_master_index_url(rewritten):
                promoted.append(rewritten)
        ranked = rank_hls_urls(promoted or urls)
        if ranked:
            LOGGER.info("play url %s", ranked[0][:180])
        return ranked

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
            # Master index is enough: we rewrite it to po./mono.m3u8 without
            # fetching the CDN from Render (that GET is always 403).
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
        """Referer only. Spoofing X-Forwarded-For with the phone IP makes
        the CDN mint a token Render cannot fetch."""
        headers = {
            "Referer": referer or CDN_PLAY_REFERER,
            "User-Agent": CDN_PLAY_USER_AGENT,
            "Origin": "https://tudeporteshoy.xyz",
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
