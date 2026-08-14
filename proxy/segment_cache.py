"""In-memory HLS segment cache with LRU eviction and single-flight fetches."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from urllib.parse import urlparse

LOGGER = logging.getLogger("hls_resolver")

SEGMENT_SUFFIXES = (".ts", ".m4s", ".m4a", ".aac", ".mp4", ".key")


def is_cacheable_segment(url: str) -> bool:
    path = (urlparse(url or "").path or "").lower()
    if not path or path.endswith(".m3u8"):
        return False
    return any(path.endswith(suffix) for suffix in SEGMENT_SUFFIXES)


class SegmentCache:
    """Share .ts bytes across viewers of the same channel.

    Concurrent requests for the same URL wait on one CDN GET (single-flight).
    Live segments only need a short TTL: the playlist window is ~15–20 s.
    """

    def __init__(self, max_bytes: int, ttl_seconds: float) -> None:
        self.max_bytes = max(0, int(max_bytes))
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self._data: OrderedDict[str, tuple[float, str, bytes]] = OrderedDict()
        self._nbytes = 0
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0
        self.stores = 0

    def stats(self) -> dict[str, int]:
        return {
            "items": len(self._data),
            "bytes": self._nbytes,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "max_bytes": self.max_bytes,
            "ttl_seconds": int(self.ttl_seconds),
        }

    def _purge_expired_unlocked(self, now: float) -> None:
        expired = [key for key, (exp, _, _) in self._data.items() if now >= exp]
        for key in expired:
            _exp, _ctype, body = self._data.pop(key)
            self._nbytes -= len(body)

    def _evict_lru_unlocked(self, needed: int) -> None:
        while self._data and self._nbytes + needed > self.max_bytes:
            _key, (_exp, _ctype, body) = self._data.popitem(last=False)
            self._nbytes -= len(body)

    def _get_unlocked(self, key: str, now: float) -> tuple[str, bytes] | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, content_type, body = item
        if now >= expires_at:
            self._data.pop(key, None)
            self._nbytes -= len(body)
            return None
        self._data.move_to_end(key)
        self.hits += 1
        if self.hits % 50 == 0:
            LOGGER.info(
                "segment cache hits=%s misses=%s items=%s bytes=%s",
                self.hits,
                self.misses,
                len(self._data),
                self._nbytes,
            )
        return content_type, body

    def _store_unlocked(self, key: str, content_type: str, body: bytes, now: float) -> None:
        if self.max_bytes <= 0 or self.ttl_seconds <= 0:
            return
        if len(body) > self.max_bytes:
            return
        previous = self._data.pop(key, None)
        if previous is not None:
            self._nbytes -= len(previous[2])
        self._evict_lru_unlocked(len(body))
        if self._nbytes + len(body) > self.max_bytes:
            return
        self._data[key] = (now + self.ttl_seconds, content_type, body)
        self._nbytes += len(body)
        self.stores += 1

    async def get(self, key: str) -> tuple[str, bytes] | None:
        async with self._lock:
            self._purge_expired_unlocked(time.time())
            return self._get_unlocked(key, time.time())

    async def get_or_load(self, key: str, loader) -> tuple[str, bytes, str]:
        """Return (content_type, body, hit|miss). One CDN fetch per key in flight."""
        now = time.time()
        async with self._lock:
            self._purge_expired_unlocked(now)
            cached = self._get_unlocked(key, time.time())
            if cached is not None:
                return cached[0], cached[1], "HIT"
            existing = self._inflight.get(key)
            if existing is None:
                loop = asyncio.get_running_loop()
                future: asyncio.Future = loop.create_future()
                self._inflight[key] = future
                owner = True
            else:
                future = existing
                owner = False

        if not owner:
            content_type, body = await asyncio.shield(future)
            self.hits += 1
            return content_type, body, "HIT"

        self.misses += 1
        try:
            content_type, body = await loader()
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            async with self._lock:
                self._store_unlocked(key, content_type, body, time.time())
            if not future.done():
                future.set_result((content_type, body))
            return content_type, body, "MISS"
        finally:
            async with self._lock:
                if self._inflight.get(key) is future:
                    self._inflight.pop(key, None)
