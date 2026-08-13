#!/usr/bin/env python3
"""Scrape one or more sports agenda pages with Playwright and emit exclusive_sources.json."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("scraper")

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
MATCH_TEXT_RE = re.compile(
    r"(\bvs\.?\b|\bv\.?\s*s\.?\b|\s[-–—]\s|\bversus\b)",
    re.IGNORECASE,
)
EVENT_HINT_TEXT_RE = re.compile(
    r"(partido|liga|copa|cup|champions|libertadores|sudamericana|mls|mx|"
    r"premier|futbol|fútbol|league)",
    re.IGNORECASE,
)
M3U8_RE = re.compile(
    r"""https?://[^\s"'<>\\]+?\.m3u8[^\s"'<>\\]*""",
    re.IGNORECASE,
)
SLUG_RE = re.compile(r"[^a-z0-9]+")
WS_RE = re.compile(r"\s+")

SKIP_HREF_PREFIXES = ("javascript:", "mailto:", "tel:", "data:")
SKIP_HOST_HINTS = (
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "t.me",
    "whatsapp.com",
)
PLAYER_HREF_RE = re.compile(
    r"(embed|player|stream|watch|live|canal|channel|video|iframe|cast|"
    r"acestream|partido|match|event|game|sport|\btv\b|/go/)",
    re.IGNORECASE,
)
NAV_PATH_RE = re.compile(
    r"/(login|signin|signup|register|contacto|contact|privacy|tos|terms|"
    r"about|cuenta|account|wp-admin|wp-login|category|tag)(/|$|\?)",
    re.IGNORECASE,
)
NAV_TEXT_RE = re.compile(
    r"^(home|inicio|contacto|contact|login|salir|menu|noticias|about|"
    r"facebook|twitter|instagram|telegram)$",
    re.IGNORECASE,
)
ONCLICK_URL_RE = re.compile(
    r"""(?:location(?:\.href)?|window\.open)\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
ANCHOR_URL_ATTRS = ("href", "data-href", "data-url", "data-link", "data-src", "data-open")
EVENT_HINT_CLASSES = (
    "event",
    "evento",
    "match",
    "partido",
    "game",
    "fixture",
    "menuitem",
    "accordion",
    "card",
)
DEFAULT_AGENDA_WAIT_SELECTORS = (
    "#menu > li",
    "#menu a.submenu-item",
    ".toggle-submenu",
    "#menu",
    "#listado",
    "#horario",
    ".menuitem",
    "[class*='menuitem' i]",
    "a[href*='en-vivo' i]",
    "a[href*='embed' i]",
    ".card-content a",
    "[id*='listado' i]",
    "[class*='listado' i]",
    "#agenda",
    "[id*='horario' i]",
    "[class*='horario' i]",
    "[class*='partido' i]",
    "[class*='event' i]",
    "details",
)
CARD_CLASS_RE = re.compile(r"card", re.IGNORECASE)
AGENDA_URL_RE = re.compile(r"""AGENDA_URL\s*=\s*["']([^"']+)["']""")
CONFIG_SCRIPT_RE = re.compile(r"""config\.js[^"']*""")


@dataclass(frozen=True)
class Settings:
    target_urls: tuple[str, ...]
    output_path: str = "exclusive_sources.json"
    timeout: float = 30.0
    max_workers: int = 8
    agenda_wait_ms: int = 3000
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
    agenda_selector: str = ""
    agenda_wait_selector: str = ""
    event_selector: str = ""
    time_selector: str = ""
    title_selector: str = ""
    channel_selector: str = ""
    use_playwright: bool = True
    stream_wait_ms: int = 10000
    proxy_base_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        target_urls = parse_target_urls(os.environ.get("TARGET_URL", ""))
        if not target_urls:
            raise SystemExit(
                "TARGET_URL is required (one URL per line in the environment variable or GitHub secret)."
            )
        use_playwright_raw = os.environ.get("USE_PLAYWRIGHT", "1")
        return cls(
            target_urls=tuple(target_urls),
            output_path=os.environ.get("OUTPUT_PATH", "exclusive_sources.json").strip(),
            timeout=float(os.environ.get("REQUEST_TIMEOUT", "30")),
            max_workers=max(1, int(os.environ.get("MAX_WORKERS", "8"))),
            agenda_wait_ms=max(0, int(os.environ.get("AGENDA_WAIT_MS", "3000"))),
            user_agent=os.environ.get("USER_AGENT", cls.user_agent).strip() or cls.user_agent,
            agenda_selector=os.environ.get("AGENDA_SELECTOR", "").strip(),
            agenda_wait_selector=os.environ.get("AGENDA_WAIT_SELECTOR", "").strip(),
            event_selector=os.environ.get("EVENT_SELECTOR", "").strip(),
            time_selector=os.environ.get("TIME_SELECTOR", "").strip(),
            title_selector=os.environ.get("TITLE_SELECTOR", "").strip(),
            channel_selector=os.environ.get("CHANNEL_SELECTOR", "").strip(),
            use_playwright=_as_bool(use_playwright_raw),
            stream_wait_ms=max(1000, int(os.environ.get("HLS_WAIT_MS", "10000"))),
            proxy_base_url=os.environ.get("PROXY_BASE_URL", "").strip().rstrip("/"),
        )


@dataclass
class AgendaEvent:
    time: str
    title: str
    category: str
    match_name: str
    channel_pages: list[str] = field(default_factory=list)
    stream_urls: list[str] = field(default_factory=list)
    source_url: str = ""


class PlaywrightRenderer:
    """Render agenda pages with Chromium so horario.js and similar scripts run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright = None
        self._browser = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SystemExit(
                "Playwright is required. Install with: pip install playwright && playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        print("[playwright] Chromium started")

    def fetch(self, url: str) -> tuple[str, dict | None]:
        if self._browser is None:
            raise RuntimeError("PlaywrightRenderer.start() must be called before fetch()")

        api_payload: dict | None = None
        page = self._browser.new_page(user_agent=self.settings.user_agent)

        def on_response(response) -> None:
            nonlocal api_payload
            if api_payload is not None:
                return
            if "agenda.json" not in response.url or not response.ok:
                return
            try:
                data = response.json()
            except Exception:
                return
            if isinstance(data, dict) and data.get("data"):
                api_payload = data
                print(
                    f"[playwright] captured agenda.json "
                    f"({len(data.get('data', []))} events) from {response.url}"
                )

        page.on("response", on_response)
        try:
            print(f"[playwright] loading {url}")
            try:
                with page.expect_response(
                    lambda response: "agenda.json" in response.url and response.ok,
                    timeout=int(self.settings.timeout * 1000),
                ):
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.timeout * 1000),
                    )
            except Exception:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=int(self.settings.timeout * 1000),
                )

            self._wait_for_agenda(page)
            menu_count = page.locator("#menu > li").count()
            print(f"[playwright] #menu items={menu_count}")
            html = page.content()
            print(f"[playwright] rendered {url} ({len(html)} bytes)")
            return html, api_payload
        finally:
            page.close()

    def _wait_for_agenda(self, page) -> None:
        selectors: list[str] = []
        for selector in (
            self.settings.agenda_wait_selector,
            self.settings.agenda_selector,
            *DEFAULT_AGENDA_WAIT_SELECTORS,
        ):
            if selector and selector not in selectors:
                selectors.append(selector)

        per_selector_ms = max(1500, int(self.settings.timeout * 1000 / max(len(selectors), 1)))
        matched = False
        for selector in selectors:
            try:
                page.wait_for_selector(selector, state="visible", timeout=per_selector_ms)
                if selector.startswith("[id*='agenda") or selector.startswith("[class*='agenda"):
                    count = page.locator(f"{selector} a").count()
                    if count == 0:
                        print(f"[playwright] skipped empty agenda selector {selector!r}")
                        continue
                print(f"[playwright] content visible via {selector!r}")
                matched = True
                break
            except Exception:
                continue

        if not matched:
            print(f"[playwright] no content selector matched; waiting {self.settings.agenda_wait_ms}ms")
            page.wait_for_timeout(self.settings.agenda_wait_ms)

        try:
            page.wait_for_function(
                """
                () => {
                  const menuItems = document.querySelectorAll('#menu > li');
                  if (menuItems.length > 0) return true;
                  const anchors = [...document.querySelectorAll('a')].filter((node) => {
                    const href = node.getAttribute('href') || '';
                    return href && href !== '#' && !href.startsWith('javascript:');
                  });
                  if (anchors.length >= 3) return true;
                  const bodyText = document.body ? document.body.innerText : '';
                  return /\\b([01]?\\d|2[0-3]):[0-5]\\d\\b/.test(bodyText);
                }
                """,
                timeout=self.settings.agenda_wait_ms + 8000,
            )
            print("[playwright] dynamic agenda menu or links detected")
        except Exception:
            print(f"[playwright] hydration fallback wait {self.settings.agenda_wait_ms}ms")
            page.wait_for_timeout(self.settings.agenda_wait_ms)

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        print("[playwright] Chromium closed")

    @property
    def browser(self):
        if self._browser is None:
            raise RuntimeError("PlaywrightRenderer.start() must be called before accessing browser")
        return self._browser


class PlaywrightHydrator:
    """Resolve direct HLS (.m3u8) URLs from channel/embed subpages using Playwright only."""

    LOG_PREFIX = "[playwright-hydrate]"
    GOTO_TIMEOUT_MS = 15000
    PLAYER_FALLBACK_WAIT_MS = 3000
    MAX_IFRAME_DEPTH = 3

    def __init__(self, renderer: PlaywrightRenderer, settings: Settings) -> None:
        self.renderer = renderer
        self.settings = settings
        self._context = None
        self._cache: dict[str, list[str]] = {}

    def start(self) -> None:
        self._context = self.renderer.browser.new_context(
            user_agent=self.settings.user_agent,
            ignore_https_errors=True,
        )
        print(f"{self.LOG_PREFIX} browser context ready")

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        print(f"{self.LOG_PREFIX} browser context closed")

    def hydrate_events(self, events: list[AgendaEvent]) -> None:
        if self._context is None:
            raise RuntimeError("PlaywrightHydrator.start() must be called before hydrate_events()")

        jobs: list[tuple[int, str, str]] = []
        seen_jobs: set[tuple[int, str]] = set()
        for index, event in enumerate(events):
            referer = event.source_url or self.settings.target_urls[0]
            for subpage_url in event.channel_pages:
                job_key = (index, subpage_url)
                if job_key in seen_jobs:
                    continue
                seen_jobs.add(job_key)
                jobs.append((index, subpage_url, referer))

        print(f"{self.LOG_PREFIX} probing {len(jobs)} subpages")
        for index, subpage_url, referer in jobs:
            print(f"{self.LOG_PREFIX} goto {subpage_url}")
            try:
                manifests = self.capture_m3u8(subpage_url, referer=referer)
            except Exception as exc:  # noqa: BLE001
                print(f"{self.LOG_PREFIX} error {subpage_url}: {exc}")
                LOGGER.warning("Playwright hydrate failed (%s): %s", subpage_url, exc)
                manifests = []

            if manifests:
                events[index].stream_urls.extend(manifests)
                print(
                    f"{self.LOG_PREFIX} resolved {len(manifests)} .m3u8 for {subpage_url}"
                )
            else:
                print(f"{self.LOG_PREFIX} discard {subpage_url}: no .m3u8 captured")

        for event in events:
            event.stream_urls = dedupe_keep_order(filter_hls_urls(event.stream_urls))
            event.channel_pages = []
            if not event.stream_urls:
                print(f"{self.LOG_PREFIX} drop {event.match_name!r}: zero .m3u8 streams")

    def capture_m3u8(
        self,
        subpage_url: str,
        referer: str | None = None,
        *,
        depth: int = 0,
        visited: set[str] | None = None,
    ) -> list[str]:
        cached = self._cache.get(subpage_url)
        if cached is not None:
            return list(cached)

        seen = visited if visited is not None else set()
        if subpage_url in seen or depth > self.MAX_IFRAME_DEPTH:
            return []
        seen.add(subpage_url)

        captured: list[str] = []
        iframe_targets: list[str] = []

        def track_manifest(raw_url: str) -> None:
            if not is_hls_url(raw_url):
                return
            if raw_url not in captured:
                captured.append(raw_url)
                print(f"{self.LOG_PREFIX} manifest {raw_url[:180]}")

        def on_response(response) -> None:
            track_manifest(response.url)
            PlaywrightHydrator._scan_response_body(response, track_manifest)

        assert self._context is not None
        page = self._context.new_page()
        if referer:
            page.set_extra_http_headers({"Referer": referer})

        page.on("response", on_response)

        try:
            print(f"{self.LOG_PREFIX} page.goto depth={depth} {subpage_url}")
            page.goto(
                subpage_url,
                wait_until="domcontentloaded",
                timeout=self.GOTO_TIMEOUT_MS,
            )

            try:
                page.wait_for_selector(
                    "iframe, video, source, .player, #player",
                    timeout=min(8000, self.settings.stream_wait_ms),
                )
            except Exception:
                pass

            try:
                page.wait_for_response(
                    lambda response: is_hls_url(response.url),
                    timeout=self.settings.stream_wait_ms,
                )
            except Exception:
                page.wait_for_timeout(self.PLAYER_FALLBACK_WAIT_MS)

            for frame in page.frames:
                frame_url = frame.url or ""
                track_manifest(frame_url)
                if not frame_url or frame_url in {"about:blank"}:
                    continue
                if frame_url != page.url and frame_url not in iframe_targets:
                    iframe_targets.append(frame_url)
                try:
                    for manifest in StreamExtractor.list_hls_in_text(frame.content()):
                        track_manifest(manifest)
                except Exception:
                    continue

            for iframe in page.locator("iframe").all()[:8]:
                src = (
                    iframe.get_attribute("src")
                    or iframe.get_attribute("data-src")
                    or iframe.get_attribute("data-lazy-src")
                )
                absolute = _abs_media_url(src, page.url)
                if absolute and absolute not in iframe_targets:
                    iframe_targets.append(absolute)
        except Exception as exc:  # noqa: BLE001
            print(f"{self.LOG_PREFIX} page failed depth={depth} {subpage_url}: {exc}")
        finally:
            page.close()

        if not filter_hls_urls(captured):
            for iframe_url in dedupe_keep_order(iframe_targets):
                print(f"{self.LOG_PREFIX} iframe depth={depth + 1} -> {iframe_url}")
                captured.extend(
                    self.capture_m3u8(
                        iframe_url,
                        referer=subpage_url,
                        depth=depth + 1,
                        visited=seen,
                    )
                )

        resolved = dedupe_keep_order(filter_hls_urls(captured))
        self._cache[subpage_url] = resolved
        return resolved

    @staticmethod
    def _scan_response_body(response, track_manifest) -> None:
        try:
            if not response.ok:
                return
            resource_type = getattr(response.request, "resource_type", "")
            content_type = (response.headers.get("content-type") or "").lower()
            if resource_type == "media" or "mpegurl" in content_type:
                track_manifest(response.url)
            if resource_type not in {"xhr", "fetch", "script", "document", "media"}:
                return
            if not any(
                token in content_type
                for token in ("json", "javascript", "text", "mpegurl", "octet-stream")
            ):
                return
            body = response.text()
            if not body or len(body) > 500_000:
                return
            for manifest in StreamExtractor.list_hls_in_text(body):
                track_manifest(manifest)
        except Exception:
            return


class HttpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            }
        )
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def get_html(self, url: str, referer: str | None = None) -> str:
        headers = {"Referer": referer} if referer else {}
        response = self.session.get(
            url,
            timeout=self.settings.timeout,
            headers=headers,
            allow_redirects=True,
        )
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        print(f"[http] {response.status_code} {url} ({len(response.text)} bytes)")
        if response.status_code >= 400 and not response.text.strip():
            response.raise_for_status()
        return response.text

    def get_json(self, url: str, referer: str | None = None) -> dict | list:
        headers = {"Accept": "application/json"}
        if referer:
            headers["Referer"] = referer
        response = self.session.get(
            url,
            timeout=self.settings.timeout,
            headers=headers,
            allow_redirects=True,
        )
        response.raise_for_status()
        print(f"[http] {response.status_code} {url} (json {len(response.text)} bytes)")
        return response.json()

    def close(self) -> None:
        self.session.close()


class AgendaApiClient:
    """Fetch the daily match agenda from agenda18 JSON used by horario.js sites."""

    def __init__(self, http: HttpClient, settings: Settings) -> None:
        self.http = http
        self.settings = settings

    def discover_api_url(self, page_url: str, html: str) -> str:
        explicit = os.environ.get("AGENDA_API_URL", "").strip()
        if explicit:
            return explicit

        soup = BeautifulSoup(html, "lxml")
        config_candidates: list[str] = []
        for script in soup.find_all("script", src=True):
            src = str(script.get("src") or "")
            if "config.js" in src:
                config_candidates.append(urljoin(page_url, src))
            if "index.js" in src:
                index_url = urljoin(page_url, src)
                try:
                    index_text = self.http.get_html(index_url, referer=page_url)
                    import_match = re.search(r"""from\s+['"](\./config\.js[^'"]*)['"]""", index_text)
                    if import_match:
                        config_candidates.append(urljoin(index_url, import_match.group(1)))
                except Exception as exc:  # noqa: BLE001
                    print(f"[api] index.js fetch failed ({index_url}): {exc}")

        for config_url in dedupe_keep_order(config_candidates):
            try:
                config_text = self.http.get_html(config_url, referer=page_url)
                match = AGENDA_URL_RE.search(config_text)
                if match:
                    print(f"[api] discovered agenda URL from {config_url}")
                    return match.group(1)
            except Exception as exc:  # noqa: BLE001
                print(f"[api] config fetch failed ({config_url}): {exc}")

        default_url = os.environ.get(
            "AGENDA_API_DEFAULT",
            "https://agenda18.com/agenda.json?v=1.07",
        )
        print(f"[api] using default agenda URL {default_url}")
        return default_url

    def fetch_events(self, api_url: str, page_url: str) -> list[AgendaEvent]:
        payload = self.http.get_json(api_url, referer=page_url)
        if not isinstance(payload, dict):
            return []
        events = parse_agenda_api(payload, page_url)
        print(f"[api] {api_url} -> {len(events)} match events")
        return events


class AgendaParser:
    """Extract events, times, and channel subpage URLs from rendered agenda HTML."""

    def __init__(self, settings: Settings, base_url: str) -> None:
        self.settings = settings
        self.base_url = base_url

    def parse(self, html: str) -> list[AgendaEvent]:
        soup = BeautifulSoup(html, "lxml")
        root = self._expand_parse_root(soup, self._agenda_root(soup))
        title = _visible_text(soup.title) if soup.title else ""
        anchor_count = len(root.find_all("a"))
        print(f"[parse] title={title!r} html={len(html)} bytes anchors={anchor_count}")
        self._debug_dump_anchors(root)

        match_events: list[AgendaEvent] = []
        menu_events = self._parse_menu_agenda(soup)
        match_events.extend(menu_events)
        if self.settings.event_selector:
            match_events.extend(self._parse_with_selectors(root))
        match_events.extend(self._parse_event_anchors(root))
        if not menu_events:
            match_events.extend(self._parse_semantic_blocks(root))
            match_events.extend(self._parse_time_proximity(root))
        link_events = self._parse_all_links(root) if not menu_events else []
        match_events = self._merge_link_fallback(match_events, link_events)
        match_events = self._dedupe_events(match_events)
        match_events = [
            event
            for event in match_events
            if event.channel_pages
            and event.category != "Canales"
            and (event.time or MATCH_TEXT_RE.search(event.match_name))
        ]

        channel_events = self._parse_channel_cards(root)
        print(
            f"[parse] matches={len(match_events)} channels={len(channel_events)} "
            f"link-fallback={len(link_events)}"
        )
        return match_events + channel_events

    def _parse_menu_agenda(self, soup: BeautifulSoup) -> list[AgendaEvent]:
        """Parse the JS-rendered #menu list built from agenda18 JSON."""
        menu = soup.select_one("#menu")
        if menu is None:
            return []

        events: list[AgendaEvent] = []
        for item in menu.select(":scope > li"):
            time_node = item.find("time")
            time_value = ""
            if time_node is not None:
                time_value = self._time_from_texts(_visible_text(time_node))
            if not time_value:
                time_value = self._time_from_texts(_visible_text(item))

            title = ""
            for span in item.select("div span"):
                text = normalize_space(_visible_text(span))
                if not text or TIME_RE.fullmatch(text):
                    continue
                if len(text) > len(title):
                    title = text

            channel_pages: list[str] = []
            for anchor in item.select("a.submenu-item, .submenu a, ul a"):
                url = self._resolve_link_url(anchor, item)
                if url and not self._is_nav_link(url, _visible_text(anchor)):
                    channel_pages.append(url)
            channel_pages = dedupe_keep_order(channel_pages)
            if not title and not channel_pages:
                continue

            sport = normalize_space(str(item.get("data-category") or ""))
            category, match_name = split_league_and_match(title or "Evento")
            if sport and category == "Sports":
                category = sport.replace("_", " ").title()

            events.append(
                AgendaEvent(
                    time=time_value,
                    title=title or match_name,
                    category=category,
                    match_name=match_name,
                    channel_pages=channel_pages,
                )
            )

        print(f"[parse] menu-agenda events={len(events)}")
        return events

    @staticmethod
    def _expand_parse_root(soup: BeautifulSoup, root: Tag) -> Tag:
        body = soup.body or soup
        root_links = len(root.find_all("a"))
        body_links = len(body.find_all("a"))
        if body_links > root_links:
            print(f"[parse] expanding parse root: {root_links} -> {body_links} anchors")
            return body
        return root

    def _debug_dump_anchors(self, root: Tag, limit: int = 30) -> None:
        print(f"[debug] first {limit} anchors (text | raw_href | resolved | parent):")
        for index, anchor in enumerate(root.find_all("a")[:limit]):
            raw_href = (anchor.get("href") or anchor.get("data-href") or anchor.get("data-url") or "").strip()
            url = self._resolve_link_url(anchor)
            label = normalize_space(_visible_text(anchor))[:100]
            parent = anchor.parent if isinstance(anchor.parent, Tag) else None
            parent_desc = self._tag_summary(parent)
            parent_text = normalize_space(_visible_text(parent))[:140] if parent else ""
            print(
                f"[debug] a[{index}] text={label!r} raw_href={raw_href!r} "
                f"resolved={url!r} parent={parent_desc!r} ctx={parent_text!r}"
            )

    @staticmethod
    def _tag_summary(node: Tag | None) -> str:
        if node is None:
            return "?"
        classes = node.get("class") or []
        class_suffix = f".{'.'.join(str(item) for item in classes[:3])}" if classes else ""
        node_id = node.get("id")
        id_suffix = f"#{node_id}" if node_id else ""
        return f"{node.name}{id_suffix}{class_suffix}"

    def _parse_event_anchors(self, root: Tag) -> list[AgendaEvent]:
        """Extract events from rendered links that mention kickoff times or match patterns."""
        containers: dict[int, Tag] = {}
        for anchor in root.find_all("a"):
            url = self._resolve_link_url(anchor)
            label = normalize_space(_visible_text(anchor))
            if not url or self._is_nav_link(url, label):
                continue
            if not self._anchor_is_event_seed(anchor):
                continue
            container = self._nearest_event_container(anchor)
            containers[id(container)] = container

        events: list[AgendaEvent] = []
        for container in containers.values():
            event = self._event_from_container(container)
            if event:
                events.append(event)
        print(f"[parse] event-anchors matched containers={len(containers)} events={len(events)}")
        return events

    def _anchor_is_event_seed(self, anchor: Tag) -> bool:
        label = normalize_space(_visible_text(anchor))
        if MATCH_TEXT_RE.search(label) or TIME_RE.search(label):
            return True
        if self._is_channel_label(label):
            return False

        parent = anchor.parent if isinstance(anchor.parent, Tag) else None
        if parent and parent.name in {"details", "summary", "option"}:
            return False

        local_context = self._local_anchor_context(anchor, depth=2)
        return self._looks_like_event_text(label, local_context)

    def _local_anchor_context(self, anchor: Tag, depth: int = 2) -> str:
        chunks: list[str] = []
        current: Tag | None = anchor
        for _ in range(depth + 1):
            if current is None:
                break
            chunks.append(normalize_space(_visible_text(current)))
            parent = current.parent
            current = parent if isinstance(parent, Tag) else None
        return normalize_space(" ".join(chunks))

    def _event_from_container(self, container: Tag) -> AgendaEvent | None:
        context = normalize_space(_visible_text(container))
        time_value = self._time_from_texts(context)
        channel_pages = self._collect_container_links(container)
        if not channel_pages:
            return None

        raw_title = self._title_from_container(container, time_value)
        if not raw_title:
            raw_title = "Evento"
        category, match_name = split_league_and_match(raw_title)
        return AgendaEvent(
            time=time_value,
            title=raw_title,
            category=category,
            match_name=match_name,
            channel_pages=channel_pages,
        )

    def _anchor_context(self, anchor: Tag) -> str:
        chunks: list[str] = [normalize_space(_visible_text(anchor))]
        current: Tag | None = anchor.parent if isinstance(anchor.parent, Tag) else None
        for _ in range(5):
            if current is None or current.name in {"body", "html"}:
                break
            chunks.append(normalize_space(_visible_text(current)))
            parent = current.parent
            current = parent if isinstance(parent, Tag) else None
        return normalize_space(" ".join(chunks))

    @staticmethod
    def _time_from_texts(*texts: str) -> str:
        for text in texts:
            match = TIME_RE.search(text or "")
            if match:
                return match.group(0)
        return ""

    def _looks_like_event_text(self, label: str, context: str) -> bool:
        combined = normalize_space(f"{label} {context}")
        if not combined:
            return False
        if self._is_channel_label(label) and not TIME_RE.search(context) and not MATCH_TEXT_RE.search(context):
            return False
        if MATCH_TEXT_RE.search(combined):
            return True
        if TIME_RE.search(combined) and (
            EVENT_HINT_TEXT_RE.search(combined) or len(label) >= 10 or PLAYER_HREF_RE.search(combined)
        ):
            return True
        if TIME_RE.search(label):
            return True
        return False

    @staticmethod
    def _is_channel_label(label: str) -> bool:
        cleaned = normalize_space(label)
        if not cleaned:
            return True
        if TIME_RE.search(cleaned) or MATCH_TEXT_RE.search(cleaned):
            return False
        return len(cleaned) <= 28

    def _title_from_container(self, container: Tag, time_value: str) -> str:
        best = ""
        for anchor in container.find_all("a"):
            label = normalize_space(_visible_text(anchor))
            if not label or self._is_channel_label(label):
                continue
            if MATCH_TEXT_RE.search(label) or (time_value and len(label) >= 8):
                if len(label) > len(best):
                    best = label
        if best:
            return TIME_RE.sub("", best).strip(" -–—|")
        text = TIME_RE.sub("", _visible_text(container))
        text = normalize_space(text)
        return text[:200].strip(" -–—|")

    def _collect_container_links(self, container: Tag, relaxed: bool = True) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in container.find_all("a"):
            url = self._resolve_link_url(anchor)
            label = normalize_space(_visible_text(anchor))
            if not url or url in seen:
                continue
            if self._is_nav_link(url, label):
                continue
            if relaxed or self._is_useful_link(url, label):
                seen.add(url)
                urls.append(url)
        return urls

    def _parse_channel_cards(self, root: Tag) -> list[AgendaEvent]:
        """Parse channel cards like 'ESPN 1' + 'Ver Canal' when no match agenda exists."""
        events: list[AgendaEvent] = []
        seen: set[str] = set()
        cards = root.select(".card-content, [class*='card-content' i]")
        if not cards:
            return events

        for card in cards:
            heading = card.find(["h2", "h3", "h4", "strong", "span"])
            channel_name = self._card_title(card) if card else ""
            if not channel_name or NAV_TEXT_RE.match(channel_name):
                continue

            urls: list[str] = []
            for anchor in card.find_all("a"):
                url = self._resolve_link_url(anchor, card)
                if url and not self._is_nav_link(url, _visible_text(anchor)):
                    urls.append(url)
            urls = dedupe_keep_order(urls)
            if not urls:
                inferred = self._infer_stream_url(channel_name)
                if inferred:
                    urls = [inferred]
            if not urls:
                continue

            key = (channel_name.lower(), urls[0])
            if key in seen:
                continue
            seen.add(key)
            events.append(
                AgendaEvent(
                    time="",
                    title=channel_name,
                    category="Canales",
                    match_name=channel_name,
                    channel_pages=urls,
                )
            )

        print(f"[parse] channel-cards events={len(events)}")
        return events

    def _infer_stream_url(self, label: str) -> str:
        slug = slugify(label)
        if not slug:
            return ""
        for path in (f"en-vivo/{slug}", f"/en-vivo/{slug}", f"embed/{slug}", f"/embed/{slug}"):
            url = self._normalize_href(path)
            if url:
                return url
        return ""

    def _is_nav_link(self, url: str, label: str = "") -> bool:
        parsed = urlparse(url)
        if NAV_PATH_RE.search(parsed.path or "/"):
            return True
        cleaned = normalize_space(label)
        if cleaned and NAV_TEXT_RE.match(cleaned):
            return True
        if not cleaned and not parsed.query:
            return True
        return False

    def _merge_link_fallback(
        self, structured: list[AgendaEvent], link_events: list[AgendaEvent]
    ) -> list[AgendaEvent]:
        if not structured:
            return link_events
        merged = list(structured)
        extra: list[AgendaEvent] = []
        for event in link_events:
            if self._urls_already_captured(event.channel_pages, merged):
                continue
            sibling = self._sibling_event(merged, event)
            if sibling is not None:
                sibling.channel_pages = dedupe_keep_order(sibling.channel_pages + event.channel_pages)
                continue
            extra.append(event)
        return merged + extra

    @staticmethod
    def _sibling_event(events: list[AgendaEvent], candidate: AgendaEvent) -> AgendaEvent | None:
        if candidate.time:
            same_time = [event for event in events if event.time == candidate.time]
            if len(same_time) == 1:
                return same_time[0]
            cand_key = slugify(candidate.match_name)
            for event in same_time:
                existing_key = slugify(event.match_name)
                if existing_key and cand_key and (existing_key in cand_key or cand_key in existing_key):
                    return event
        return None

    @staticmethod
    def _urls_already_captured(urls: list[str], events: list[AgendaEvent]) -> bool:
        captured = {url for event in events for url in event.channel_pages}
        return bool(urls) and all(url in captured for url in urls)

    def _agenda_root(self, soup: BeautifulSoup) -> Tag:
        if self.settings.agenda_selector:
            node = soup.select_one(self.settings.agenda_selector)
            if node:
                return node

        candidates: list[Tag] = []
        for selector in (
            "#menu",
            "#listado",
            "#horario",
            "[class*='menuitem' i]",
            "[id*='listado' i]",
            "[class*='listado' i]",
            "main",
        ):
            node = soup.select_one(selector)
            if node:
                candidates.append(node)

        for node in soup.select("[id*='agenda' i], [class*='agenda' i], [id*='horario' i], [class*='horario' i]"):
            if node.name in {"h1", "h2", "h3", "h4", "h5", "h6"} and not node.find("a"):
                continue
            if len(node.find_all("a")) == 0 and not TIME_RE.search(_visible_text(node)):
                continue
            candidates.append(node)

        if candidates:
            return max(
                candidates,
                key=lambda node: (len(node.find_all("a")), len(_visible_text(node))),
            )
        return soup.body or soup

    def _parse_with_selectors(self, root: Tag) -> list[AgendaEvent]:
        events: list[AgendaEvent] = []
        for block in root.select(self.settings.event_selector):
            event = self._event_from_block(block)
            if event:
                events.append(event)
        return events

    def _parse_semantic_blocks(self, root: Tag) -> list[AgendaEvent]:
        events: list[AgendaEvent] = []
        seen: set[int] = set()
        candidates = list(root.find_all("details"))
        for hint in EVENT_HINT_CLASSES:
            candidates.extend(root.select(f"[class*='{hint}' i]"))
        for table_row in root.select("tr"):
            if TIME_RE.search(_visible_text(table_row)):
                candidates.append(table_row)

        for block in candidates:
            marker = id(block)
            if marker in seen or self._looks_like_event_list(block):
                continue
            seen.add(marker)
            event = self._event_from_block(block)
            if event:
                events.append(event)
        return events

    def _parse_time_proximity(self, root: Tag) -> list[AgendaEvent]:
        events: list[AgendaEvent] = []
        for text_node in root.find_all(string=TIME_RE):
            time_match = TIME_RE.search(str(text_node))
            if not time_match:
                continue
            container = text_node.parent
            for _ in range(5):
                if not container or not isinstance(container, Tag):
                    break
                if any(self._resolve_link_url(anchor) for anchor in self._iter_anchors(container)):
                    event = self._event_from_block(container, fallback_time=time_match.group(0))
                    if event:
                        events.append(event)
                    break
                container = container.parent
        return events

    @staticmethod
    def _looks_like_event_list(block: Tag) -> bool:
        times = {match.group(0) for match in TIME_RE.finditer(_visible_text(block))}
        return len(times) > 1

    def _parse_all_links(self, root: Tag) -> list[AgendaEvent]:
        grouped: dict[int, tuple[Tag, list[str]]] = {}
        for anchor in root.find_all("a"):
            url = self._resolve_link_url(anchor)
            label = normalize_space(_visible_text(anchor))
            if not url or self._is_nav_link(url, label):
                continue
            if not (
                self._anchor_is_event_seed(anchor)
                or self._is_useful_link(url, label)
            ):
                continue
            container = self._nearest_event_container(anchor)
            key = id(container)
            if key not in grouped:
                grouped[key] = (container, [])
            grouped[key][1].append(url)

        events: list[AgendaEvent] = []
        for container, urls in grouped.values():
            urls = dedupe_keep_order(urls)
            if not urls:
                continue
            if container.name in {"body", "html"} or self._looks_like_event_list(container):
                events.extend(self._events_from_standalone_links(container, urls))
                continue
            event = self._event_from_container(container)
            if event:
                event.channel_pages = dedupe_keep_order(event.channel_pages + urls)
                events.append(event)
                continue
            events.extend(self._events_from_standalone_links(container, urls))
        if not events:
            all_urls = [url for _, urls in grouped.values() for url in urls]
            events = self._events_from_standalone_links(root, dedupe_keep_order(all_urls))
        return events

    def _events_from_standalone_links(self, container: Tag, urls: list[str]) -> list[AgendaEvent]:
        events: list[AgendaEvent] = []
        for url in urls:
            anchor = None
            for candidate in container.find_all("a"):
                if self._resolve_link_url(candidate) == url:
                    anchor = candidate
                    break
            label = normalize_space(_visible_text(anchor) if anchor else "")
            nearby = normalize_space(_visible_text(anchor.parent if anchor and anchor.parent else container))
            raw_title = label or nearby or url
            raw_title = TIME_RE.sub("", raw_title).strip(" -–—|") or label or "Evento"
            time_value = ""
            if anchor:
                time_value = self._extract_time(anchor.parent if isinstance(anchor.parent, Tag) else container)
            if not time_value:
                time_match = TIME_RE.search(label) or TIME_RE.search(nearby)
                time_value = time_match.group(0) if time_match else ""
            category, match_name = split_league_and_match(raw_title)
            events.append(
                AgendaEvent(
                    time=time_value,
                    title=raw_title,
                    category=category,
                    match_name=match_name,
                    channel_pages=[url],
                )
            )
        return events

    def _nearest_event_container(self, node: Tag) -> Tag:
        current: Tag | None = node
        for _ in range(8):
            if current is None:
                break
            if current.name in {"li", "tr", "article", "details", "section"}:
                return current
            if TIME_RE.search(_visible_text(current)) and not self._looks_like_event_list(current):
                return current
            parent = current.parent
            if not isinstance(parent, Tag) or parent.name in {"body", "html"}:
                return current
            current = parent
        return node

    def _event_from_block(self, block: Tag, fallback_time: str = "") -> AgendaEvent | None:
        time_value = self._extract_time(block) or fallback_time
        raw_title = self._extract_title(block)
        relaxed = self._block_looks_like_event(block)
        channel_pages = self._extract_channel_urls(block, relaxed=relaxed)
        if not raw_title and not channel_pages:
            return None
        if not raw_title:
            raw_title = self._title_from_container(block, time_value) or "Evento"
        if not channel_pages:
            return None

        category, match_name = split_league_and_match(raw_title)
        return AgendaEvent(
            time=time_value,
            title=raw_title,
            category=category,
            match_name=match_name,
            channel_pages=channel_pages,
        )

    @staticmethod
    def _block_looks_like_event(block: Tag) -> bool:
        text = _visible_text(block)
        return bool(TIME_RE.search(text) or MATCH_TEXT_RE.search(text) or EVENT_HINT_TEXT_RE.search(text))

    def _extract_time(self, block: Tag) -> str:
        if self.settings.time_selector:
            node = block.select_one(self.settings.time_selector)
            if node:
                match = TIME_RE.search(_visible_text(node))
                if match:
                    return match.group(0)
        for selector in ("time", "[class*='time' i]", "[class*='hora' i]", "[class*='hour' i]"):
            node = block.select_one(selector)
            if node:
                match = TIME_RE.search(_visible_text(node))
                if match:
                    return match.group(0)
        match = TIME_RE.search(_visible_text(block))
        return match.group(0) if match else ""

    def _extract_title(self, block: Tag) -> str:
        if self.settings.title_selector:
            node = block.select_one(self.settings.title_selector)
            if node:
                return normalize_space(_visible_text(node))
        for selector in (
            "h1",
            "h2",
            "h3",
            "h4",
            "[class*='title' i]",
            "[class*='name' i]",
            "[class*='event' i]",
            "[class*='partido' i]",
            "[class*='match' i]",
            "summary",
            "strong",
            "b",
        ):
            node = block.select_one(selector)
            if not node:
                continue
            text = normalize_space(_visible_text(node))
            text = TIME_RE.sub("", text).strip(" -–—|")
            if text:
                return text
        text = normalize_space(_visible_text(block))
        text = TIME_RE.sub("", text)
        text = re.split(r"\n+", text, maxsplit=1)[0]
        return normalize_space(text).strip(" -–—|")

    def _extract_channel_urls(self, block: Tag, relaxed: bool = False) -> list[str]:
        if relaxed:
            return self._collect_container_links(block, relaxed=True)
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in self._iter_anchors(block):
            absolute = self._resolve_link_url(anchor)
            if not absolute or absolute in seen:
                continue
            if not self._is_useful_link(absolute, _visible_text(anchor)):
                continue
            seen.add(absolute)
            urls.append(absolute)
        return urls

    @staticmethod
    def _iter_anchors(block: Tag) -> list[Tag]:
        anchors: list[Tag] = []
        if block.name == "a":
            anchors.append(block)
        anchors.extend(block.find_all("a"))
        return anchors

    def _href_from_anchor(self, anchor: Tag) -> str:
        for attr in ANCHOR_URL_ATTRS:
            absolute = self._normalize_href(str(anchor.get(attr) or ""))
            if absolute:
                return absolute
        onclick = str(anchor.get("onclick") or "")
        match = ONCLICK_URL_RE.search(onclick)
        if match:
            return self._normalize_href(match.group(1))
        return ""

    def _resolve_link_url(self, anchor: Tag, container: Tag | None = None) -> str:
        url = self._href_from_anchor(anchor)
        if url:
            return url

        scope = container or anchor
        for node in [scope, *scope.parents]:
            if not isinstance(node, Tag):
                continue
            for attr in ("data-href", "data-url", "data-link", "data-src"):
                url = self._normalize_href(str(node.get(attr) or ""))
                if url:
                    return url
            onclick = str(node.get("onclick") or "")
            match = ONCLICK_URL_RE.search(onclick)
            if match:
                url = self._normalize_href(match.group(1))
                if url:
                    return url

        card = anchor.find_parent(class_=CARD_CLASS_RE)
        if card is None and container is not None:
            card = container if CARD_CLASS_RE.search(" ".join(container.get("class") or [])) else None
        if card is not None:
            inferred = self._infer_stream_url(self._card_title(card))
            if inferred:
                return inferred
        return ""

    @staticmethod
    def _card_title(card: Tag) -> str:
        heading = card.find(["h2", "h3", "h4", "strong"])
        if heading:
            title = normalize_space(_visible_text(heading))
            if title:
                return title
        text = normalize_space(_visible_text(card))
        text = re.sub(r"(?i)\bver canal\b.*", "", text)
        text = re.sub(r"(?i)\bonline\b.*", "", text)
        text = re.sub(r"(?i)\ben vivo\b.*", "", text)
        return normalize_space(text.split(".")[0])[:80]

    def _is_useful_link(self, url: str, label: str = "") -> bool:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if NAV_PATH_RE.search(path):
            return False
        if label and NAV_TEXT_RE.match(normalize_space(label)):
            return False
        if PLAYER_HREF_RE.search(url) or parsed.query:
            return True
        base_host = urlparse(self.base_url).netloc.lower()
        if parsed.netloc.lower() != base_host:
            return True
        normalized_path = path.rstrip("/")
        if not normalized_path:
            return False
        return normalized_path != urlparse(self.base_url).path.rstrip("/")

    def _normalize_href(self, href: str) -> str:
        href = (href or "").strip()
        if not href or href.startswith("#"):
            return ""
        lowered = href.lower()
        if lowered.startswith(SKIP_HREF_PREFIXES):
            return ""
        absolute = urljoin(self.base_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        if any(hint in host for hint in SKIP_HOST_HINTS):
            return ""
        if absolute.rstrip("/") == self.base_url.rstrip("/"):
            return ""
        return absolute

    @staticmethod
    def _dedupe_events(events: list[AgendaEvent]) -> list[AgendaEvent]:
        unique: dict[tuple[str, str], AgendaEvent] = {}
        order: list[tuple[str, str]] = []
        for event in events:
            key = event_merge_key(event)
            existing = unique.get(key)
            if existing is None:
                unique[key] = AgendaEvent(
                    time=event.time,
                    title=event.title,
                    category=event.category,
                    match_name=event.match_name,
                    channel_pages=list(event.channel_pages),
                    stream_urls=list(event.stream_urls),
                    source_url=event.source_url,
                )
                order.append(key)
                continue
            existing.channel_pages = dedupe_keep_order(existing.channel_pages + event.channel_pages)
        return [unique[key] for key in order]


class StreamExtractor:
    """Pull HLS manifest URLs from HTML (Playwright handles live network capture)."""

    def extract_hls_from_html(self, html: str, page_url: str) -> list[str]:
        found: list[str] = []
        found.extend(self.list_hls_in_text(html))
        for node_src in self._video_sources(html, page_url):
            if is_hls_url(node_src):
                found.append(node_src)
        return dedupe_keep_order(filter_hls_urls(found))

    def extract_iframe_urls(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        return self._iframes(soup, page_url)

    @staticmethod
    def list_hls_in_text(text: str) -> list[str]:
        return [match.group(0).rstrip("\\") for match in M3U8_RE.finditer(text)]

    def _video_sources(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        urls: list[str] = []
        for node in soup.find_all(["video", "source"]):
            src = node.get("src") or node.get("data-src")
            absolute = _abs_media_url(src, page_url)
            if absolute:
                urls.append(absolute)
        return urls

    def _iframes(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        urls: list[str] = []
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src") or iframe.get("data-lazy-src")
            absolute = _abs_media_url(src, page_url)
            if absolute:
                urls.append(absolute)
        return urls


class SourceBuilder:
    def build(self, events: Iterable[AgendaEvent], settings: Settings) -> list[dict]:
        if settings.proxy_base_url:
            return self._build_proxy_records(events, settings)
        return self._build_direct_records(events)

    def _build_direct_records(self, events: Iterable[AgendaEvent]) -> list[dict]:
        records: list[dict] = []
        used_ids: dict[str, int] = {}
        for event in events:
            urls = dedupe_keep_order(filter_hls_urls(event.stream_urls))
            if not urls:
                print(f"[build] skip {event.match_name!r}: no .m3u8 resolved")
                continue
            records.append(self._make_record(event, urls, used_ids))
        return records

    def _build_proxy_records(self, events: Iterable[AgendaEvent], settings: Settings) -> list[dict]:
        records: list[dict] = []
        used_ids: dict[str, int] = {}
        for event in events:
            embed_pages = dedupe_keep_order(event.channel_pages)
            if not embed_pages:
                print(f"[build] skip {event.match_name!r}: no embed pages for proxy")
                continue
            referer = event.source_url or settings.target_urls[0]
            urls = [
                build_proxy_play_url(settings.proxy_base_url, embed_url, referer)
                for embed_url in embed_pages
            ]
            records.append(self._make_record(event, urls, used_ids))
        return records

    def _make_record(
        self,
        event: AgendaEvent,
        urls: list[str],
        used_ids: dict[str, int],
    ) -> dict:
        base_id = slugify(event.match_name) or slugify(event.title) or "event"
        used_ids[base_id] = used_ids.get(base_id, 0) + 1
        suffix = event.time.replace(":", "") if event.time else str(used_ids[base_id])
        record_id = base_id if used_ids[base_id] == 1 else f"{base_id}-{suffix}"
        name = f"{event.time} - {event.match_name}".strip(" -") if event.time else event.match_name
        return {
            "id": record_id,
            "name": name,
            "category": event.category or "Sports",
            "type": "hls",
            "urls": urls,
        }


class Scraper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = HttpClient(settings)
        self.renderer = PlaywrightRenderer(settings)
        self.builder = SourceBuilder()

    def run(self) -> list[dict]:
        print(f"[run] TARGET_URL count={len(self.settings.target_urls)}")
        for agenda_url in self.settings.target_urls:
            print(f"[run] site={agenda_url}")

        self.renderer.start()
        hydrator: PlaywrightHydrator | None = None
        if not self.settings.proxy_base_url:
            hydrator = PlaywrightHydrator(self.renderer, self.settings)
            hydrator.start()
        else:
            print(
                f"[run] proxy mode: skipping CI .m3u8 capture, "
                f"urls will point to {self.settings.proxy_base_url}"
            )

        api_client = AgendaApiClient(self.http, self.settings)
        global_api_matches: list[AgendaEvent] = []
        captured_api_payload: dict | None = None

        collected: list[AgendaEvent] = []
        try:
            for agenda_url in self.settings.target_urls:
                try:
                    events, api_payload = self._scrape_agenda(agenda_url)
                    if api_payload and captured_api_payload is None:
                        captured_api_payload = api_payload
                except Exception as exc:  # noqa: BLE001
                    print(f"[agenda] {agenda_url} -> ERROR: {exc}")
                    LOGGER.warning("Agenda failed (%s): %s", agenda_url, exc)
                    continue
                print(f"[agenda] {agenda_url} -> {len(events)} eventos")
                collected.extend(events)

            matches_in_collected = [event for event in collected if event.category != "Canales"]
            if not matches_in_collected:
                if captured_api_payload:
                    referer = self.settings.target_urls[0]
                    global_api_matches = parse_agenda_api(captured_api_payload, referer)
                    print(f"[api] playwright captured -> {len(global_api_matches)} partidos")

                if not global_api_matches:
                    referer = self.settings.target_urls[0]
                    page_html = self.http.get_html(referer)
                    api_url = api_client.discover_api_url(referer, page_html)
                    try:
                        global_api_matches = api_client.fetch_events(api_url, referer)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[api] global agenda fetch failed ({api_url}): {exc}")

            channels = [event for event in collected if event.category == "Canales"]
            matches = [event for event in collected if event.category != "Canales"]
            matches = merge_events(matches + global_api_matches)
            channels = merge_events(channels)
            events = matches + channels
            print(
                f"[run] combined unique partidos={len(matches)} canales={len(channels)}"
            )
            if hydrator is not None:
                self._hydrate_streams(events, hydrator)
        finally:
            if hydrator is not None:
                hydrator.close()
            self.renderer.close()

        records = self.builder.build(events, self.settings)
        write_json(self.settings.output_path, records)
        print(f"[run] wrote {len(records)} sources to {self.settings.output_path}")
        return records

    def _scrape_agenda(self, agenda_url: str) -> tuple[list[AgendaEvent], dict | None]:
        agenda_html, api_payload = self._fetch_agenda_html(agenda_url)
        parser = AgendaParser(self.settings, agenda_url)
        html_events = parser.parse(agenda_html)

        channels = [event for event in html_events if event.category == "Canales"]
        matches = [event for event in html_events if event.category != "Canales"]

        if api_payload:
            api_matches = parse_agenda_api(api_payload, agenda_url)
            print(f"[api] page captured -> {len(api_matches)} partidos")
            matches = merge_events(matches + api_matches)
        else:
            api_client = AgendaApiClient(self.http, self.settings)
            api_url = api_client.discover_api_url(agenda_url, agenda_html)
            try:
                api_matches = api_client.fetch_events(api_url, agenda_url)
                matches = merge_events(matches + api_matches)
            except Exception as exc:  # noqa: BLE001
                print(f"[api] agenda fetch failed ({api_url}): {exc}")
                LOGGER.warning("Agenda API failed (%s): %s", api_url, exc)

        events = matches + channels
        for event in events:
            event.source_url = agenda_url
            event.channel_pages = dedupe_keep_order(event.channel_pages)
        print(f"[agenda] {agenda_url} -> {len(matches)} partidos + {len(channels)} canales")
        return events, api_payload

    def _fetch_agenda_html(self, agenda_url: str) -> tuple[str, dict | None]:
        if self.settings.use_playwright:
            return self.renderer.fetch(agenda_url)
        return self.http.get_html(agenda_url), None

    def _hydrate_streams(self, events: list[AgendaEvent], hydrator: PlaywrightHydrator) -> None:
        hydrator.hydrate_events(events)

    def close(self) -> None:
        self.http.close()


def format_diary_hour(raw_value: str) -> str:
    value = normalize_space(raw_value)
    if not value:
        return ""
    match = TIME_RE.search(value)
    if match:
        return match.group(0)
    cleaned = value.replace("Z", "+00:00")
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(cleaned)
        return parsed.strftime("%H:%M")
    except ValueError:
        return ""


def parse_agenda_api(payload: dict, base_url: str) -> list[AgendaEvent]:
    events: list[AgendaEvent] = []
    for item in payload.get("data", []):
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes") or {}
        description = normalize_space(str(attrs.get("diary_description") or ""))
        if not description:
            continue

        time_value = format_diary_hour(str(attrs.get("diary_hour") or ""))
        sport = normalize_space(str(attrs.get("deportes") or ""))
        category, match_name = split_league_and_match(description)
        if sport and category == "Sports":
            category = sport.replace("_", " ").title()

        channel_pages: list[str] = []
        embeds = (attrs.get("embeds") or {}).get("data") or []
        for embed in embeds:
            if not isinstance(embed, dict):
                continue
            embed_attrs = embed.get("attributes") or {}
            iframe = normalize_space(str(embed_attrs.get("embed_iframe") or ""))
            embed_name = normalize_space(str(embed_attrs.get("embed_name") or ""))
            if not iframe:
                continue
            if iframe.startswith("http"):
                channel_pages.append(iframe)
            else:
                channel_pages.append(urljoin(base_url, iframe))
            if embed_name:
                print(f"[api] embed {embed_name} -> {channel_pages[-1]}")
        channel_pages = dedupe_keep_order(channel_pages)
        if not channel_pages:
            continue

        events.append(
            AgendaEvent(
                time=time_value,
                title=description,
                category=category,
                match_name=match_name,
                channel_pages=channel_pages,
            )
        )
    return events


def parse_target_urls(raw: str) -> list[str]:
    """Split TARGET_URL on newlines, trim whitespace, and drop empty lines."""
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")
    urls: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        url = line.strip().strip("'\"")
        if not url or url.startswith("#"):
            continue
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def event_merge_key(event: AgendaEvent) -> tuple[str, str]:
    match_key = slugify(event.match_name) or event.match_name.strip().lower()
    return (event.time, match_key)


def merge_events(events: Iterable[AgendaEvent]) -> list[AgendaEvent]:
    """Merge the same match (time + name) across pages and union unique links."""
    merged: dict[tuple[str, str], AgendaEvent] = {}
    order: list[tuple[str, str]] = []
    for event in events:
        key = event_merge_key(event)
        existing = merged.get(key)
        if existing is None:
            merged[key] = AgendaEvent(
                time=event.time,
                title=event.title,
                category=event.category,
                match_name=event.match_name,
                channel_pages=list(event.channel_pages),
                stream_urls=list(event.stream_urls),
                source_url=event.source_url,
            )
            order.append(key)
            continue
        if existing.category == "Sports" and event.category != "Sports":
            existing.category = event.category
            existing.title = event.title
        existing.channel_pages = dedupe_keep_order(existing.channel_pages + event.channel_pages)
        existing.stream_urls = dedupe_keep_order(existing.stream_urls + event.stream_urls)
    return [merged[key] for key in order]


def split_league_and_match(title: str) -> tuple[str, str]:
    cleaned = normalize_space(title)
    if ":" in cleaned:
        league, match = cleaned.split(":", 1)
        league, match = league.strip(), match.strip()
        if league and match:
            return league, match
    return "Sports", cleaned


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = SLUG_RE.sub("-", ascii_text.lower()).strip("-")
    return slug[:80]


def normalize_space(value: str) -> str:
    return WS_RE.sub(" ", value or "").strip()


def _visible_text(node: Tag) -> str:
    return node.get_text(" ", strip=True)


def _abs_media_url(src: str | None, page_url: str) -> str:
    if not src:
        return ""
    src = src.strip()
    if not src or src.startswith("about:") or src.startswith("javascript:"):
        return ""
    absolute = urljoin(page_url, src)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return absolute


def is_hls_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower().strip()
    if lower.startswith(("blob:", "data:", "about:")):
        return False
    return ".m3u8" in lower


def build_proxy_play_url(proxy_base_url: str, embed_url: str, referer: str) -> str:
    query = f"embed={quote(embed_url, safe='')}&referer={quote(referer, safe='')}"
    return f"{proxy_base_url.rstrip('/')}/v1/play.m3u8?{query}"


def filter_hls_urls(urls: Iterable[str]) -> list[str]:
    return [url for url in urls if is_hls_url(url)]


def _all_hls(urls: list[str]) -> bool:
    return bool(urls) and all(".m3u8" in url.lower() for url in urls)


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def write_json(path: str, payload: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def configure_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> int:
    configure_logging()
    settings = Settings.from_env()
    scraper = Scraper(settings)
    try:
        scraper.run()
    finally:
        scraper.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
