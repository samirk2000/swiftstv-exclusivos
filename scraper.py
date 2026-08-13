#!/usr/bin/env python3
"""Scrape one or more sports agenda HTML pages and emit exclusive_sources.json."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("scraper")

TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")
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
CHANNEL_HINT_CLASSES = (
    "channel",
    "canal",
    "option",
    "opcion",
    "opción",
    "embed",
    "stream",
    "links",
    "dropdown",
    "collapse",
    "accordion-body",
    "menuitem-content",
)


@dataclass(frozen=True)
class Settings:
    target_urls: tuple[str, ...]
    output_path: str = "exclusive_sources.json"
    timeout: float = 20.0
    max_workers: int = 8
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
    agenda_selector: str = ""
    event_selector: str = ""
    time_selector: str = ""
    title_selector: str = ""
    channel_selector: str = ""
    use_playwright: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        target_urls = parse_target_urls(os.environ.get("TARGET_URL", ""))
        if not target_urls:
            raise SystemExit(
                "TARGET_URL is required (one URL per line in the environment variable or GitHub secret)."
            )
        return cls(
            target_urls=tuple(target_urls),
            output_path=os.environ.get("OUTPUT_PATH", "exclusive_sources.json").strip(),
            timeout=float(os.environ.get("REQUEST_TIMEOUT", "20")),
            max_workers=max(1, int(os.environ.get("MAX_WORKERS", "8"))),
            user_agent=os.environ.get("USER_AGENT", cls.user_agent).strip() or cls.user_agent,
            agenda_selector=os.environ.get("AGENDA_SELECTOR", "").strip(),
            event_selector=os.environ.get("EVENT_SELECTOR", "").strip(),
            time_selector=os.environ.get("TIME_SELECTOR", "").strip(),
            title_selector=os.environ.get("TITLE_SELECTOR", "").strip(),
            channel_selector=os.environ.get("CHANNEL_SELECTOR", "").strip(),
            use_playwright=_as_bool(os.environ.get("USE_PLAYWRIGHT", "")),
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
        if response.status_code >= 400:
            LOGGER.warning("HTTP %s for %s", response.status_code, url)
            if not response.text.strip():
                response.raise_for_status()
        return response.text

    def close(self) -> None:
        self.session.close()


class AgendaParser:
    """Extract events, times, and channel subpage URLs from the agenda HTML."""

    def __init__(self, settings: Settings, base_url: str) -> None:
        self.settings = settings
        self.base_url = base_url

    def parse(self, html: str) -> list[AgendaEvent]:
        soup = BeautifulSoup(html, "lxml")
        root = self._agenda_root(soup)
        title = _visible_text(soup.title) if soup.title else ""
        anchor_count = len(soup.find_all("a"))
        print(f"[parse] title={title!r} html={len(html)} bytes anchors={anchor_count}")

        events = self._parse_with_selectors(root) if self.settings.event_selector else []
        strategy = "selectors" if events else ""
        if not events:
            events = self._parse_semantic_blocks(root)
            strategy = "semantic" if events else strategy
        if not events:
            events = self._parse_time_proximity(root)
            strategy = "time-proximity" if events else strategy
        link_events = self._parse_all_links(root)
        print(
            f"[parse] strategy={strategy or 'none'} structured={len(events)} "
            f"link-fallback={len(link_events)}"
        )
        events = self._merge_link_fallback(events, link_events)
        print(f"[parse] strategy={strategy or 'all-links' if events else 'none'} events={len(events)}")
        return self._dedupe_events(events)

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
        for selector in (
            "[id*='agenda' i]",
            "[class*='agenda' i]",
            "[id*='schedule' i]",
            "[class*='schedule' i]",
            "main",
        ):
            node = soup.select_one(selector)
            if node:
                return node
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
        """Fallback: pair each HH:MM text node with the nearest title and links."""
        events: list[AgendaEvent] = []
        for text_node in root.find_all(string=TIME_RE):
            time_match = TIME_RE.search(str(text_node))
            if not time_match:
                continue
            container = text_node.parent
            for _ in range(5):
                if not container or not isinstance(container, Tag):
                    break
                if any(self._href_from_anchor(anchor) for anchor in self._iter_anchors(container)):
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
        """Last resort: group every useful <a> (subpage / player) into events."""
        grouped: dict[int, tuple[Tag, list[str]]] = {}
        for anchor in root.find_all("a"):
            url = self._href_from_anchor(anchor)
            if not url or not self._is_useful_link(url, _visible_text(anchor)):
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
            event = self._event_from_block(container)
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
                if self._href_from_anchor(candidate) == url:
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
        channel_pages = self._extract_channel_urls(block)
        if not raw_title and not channel_pages:
            return None
        if not raw_title:
            raw_title = "Evento"
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

    def _extract_channel_urls(self, block: Tag) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in self._iter_anchors(block):
            absolute = self._href_from_anchor(anchor)
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
        unique: dict[tuple[str, str, tuple[str, ...]], AgendaEvent] = {}
        for event in events:
            key = (event.time, event.match_name.lower(), tuple(event.channel_pages))
            unique.setdefault(key, event)
        return list(unique.values())


class StreamExtractor:
    """Pull iframe src / publicly listed HLS URLs from a channel subpage."""

    def extract(self, html: str, page_url: str) -> list[str]:
        soup = BeautifulSoup(html, "lxml")
        found: list[str] = []
        found.extend(self._iframes(soup, page_url))
        found.extend(self._video_sources(soup, page_url))
        found.extend(self._listed_hls(html))
        return dedupe_keep_order(found)

    def _iframes(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        urls: list[str] = []
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src") or iframe.get("data-lazy-src")
            absolute = _abs_media_url(src, page_url)
            if absolute:
                urls.append(absolute)
        return urls

    def _video_sources(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        urls: list[str] = []
        for node in soup.find_all(["video", "source"]):
            src = node.get("src") or node.get("data-src")
            absolute = _abs_media_url(src, page_url)
            if absolute:
                urls.append(absolute)
        return urls

    @staticmethod
    def _listed_hls(html: str) -> list[str]:
        return [match.group(0).rstrip("\\") for match in M3U8_RE.finditer(html)]


class SourceBuilder:
    def build(self, events: Iterable[AgendaEvent]) -> list[dict]:
        records: list[dict] = []
        used_ids: dict[str, int] = {}
        for event in events:
            urls = dedupe_keep_order(event.stream_urls or event.channel_pages)
            if not urls:
                continue
            base_id = slugify(event.match_name) or slugify(event.title) or "event"
            used_ids[base_id] = used_ids.get(base_id, 0) + 1
            suffix = event.time.replace(":", "") if event.time else str(used_ids[base_id])
            record_id = base_id if used_ids[base_id] == 1 else f"{base_id}-{suffix}"
            source_type = "hls" if _all_hls(urls) else "embed"
            name = f"{event.time} - {event.match_name}".strip(" -") if event.time else event.match_name
            records.append(
                {
                    "id": record_id,
                    "name": name,
                    "category": event.category or "Sports",
                    "type": source_type,
                    "urls": urls,
                }
            )
        return records


class Scraper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = HttpClient(settings)
        self.streams = StreamExtractor()
        self.builder = SourceBuilder()
        self._stream_cache: dict[str, list[str]] = {}
        self._stream_lock = threading.Lock()

    def run(self) -> list[dict]:
        print(f"[run] TARGET_URL count={len(self.settings.target_urls)}")
        for agenda_url in self.settings.target_urls:
            print(f"[run] site={agenda_url}")
        collected: list[AgendaEvent] = []
        for agenda_url in self.settings.target_urls:
            try:
                events = self._scrape_agenda(agenda_url)
            except Exception as exc:  # noqa: BLE001 - continue with the remaining pages
                print(f"[agenda] {agenda_url} -> ERROR: {exc}")
                LOGGER.warning("Agenda failed (%s): %s", agenda_url, exc)
                continue
            print(f"[agenda] {agenda_url} -> {len(events)} eventos")
            collected.extend(events)

        events = merge_events(collected)
        print(f"[run] combined unique events={len(events)}")
        self._hydrate_streams(events)
        records = self.builder.build(events)
        write_json(self.settings.output_path, records)
        print(f"[run] wrote {len(records)} sources to {self.settings.output_path}")
        return records

    def _scrape_agenda(self, agenda_url: str) -> list[AgendaEvent]:
        LOGGER.info("Fetching agenda from %s", agenda_url)
        agenda_html = self._fetch_agenda_html(agenda_url)
        events = AgendaParser(self.settings, agenda_url).parse(agenda_html)
        for event in events:
            event.source_url = agenda_url
            event.channel_pages = dedupe_keep_order(event.channel_pages)
        return events

    def _fetch_agenda_html(self, agenda_url: str) -> str:
        if self.settings.use_playwright:
            return fetch_with_playwright(agenda_url, self.settings.timeout)
        return self.http.get_html(agenda_url)

    def _hydrate_streams(self, events: list[AgendaEvent]) -> None:
        jobs: list[tuple[int, str, str]] = []
        seen_jobs: set[tuple[int, str]] = set()
        for index, event in enumerate(events):
            referer = event.source_url or self.settings.target_urls[0]
            for url in event.channel_pages:
                job_key = (index, url)
                if job_key in seen_jobs:
                    continue
                seen_jobs.add(job_key)
                jobs.append((index, url, referer))

        print(f"[hydrate] channel subpages={len(jobs)}")
        with ThreadPoolExecutor(max_workers=self.settings.max_workers) as pool:
            future_map = {
                pool.submit(self._extract_from_channel, url, referer): (index, url)
                for index, url, referer in jobs
            }
            for future in as_completed(future_map):
                index, channel_url = future_map[future]
                try:
                    urls = future.result()
                except Exception as exc:  # noqa: BLE001 - keep the rest of the run going
                    print(f"[channel] FAIL {channel_url} -> {exc}")
                    LOGGER.warning("Channel page failed: %s", exc)
                    events[index].stream_urls.append(channel_url)
                    continue
                events[index].stream_urls.extend(urls)

        for event in events:
            event.stream_urls = dedupe_keep_order(event.stream_urls or event.channel_pages)

    def _extract_from_channel(self, url: str, referer: str | None = None) -> list[str]:
        with self._stream_lock:
            cached = self._stream_cache.get(url)
        if cached is not None:
            return list(cached)

        html = self.http.get_html(url, referer=referer)
        extracted = self.streams.extract(html, url)
        resolved = extracted or [url]
        with self._stream_lock:
            self._stream_cache[url] = resolved
        return list(resolved)

    def close(self) -> None:
        self.http.close()


def fetch_with_playwright(url: str, timeout: float) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("USE_PLAYWRIGHT=1 requires the playwright package and browsers installed.") from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        page.wait_for_timeout(1500)
        html = page.content()
        browser.close()
        return html


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
