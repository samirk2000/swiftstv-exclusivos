"""Unit tests for RAM segment cache and Playwright token reuse."""

from __future__ import annotations

import asyncio
import time
import unittest

from hls_resolver import (
    HlsResolver,
    HlsResolverSettings,
    manifest_urls_expire_at,
    raw_query_value,
    token_expiry_unix,
)
from segment_cache import SegmentCache, is_cacheable_segment


class SegmentHelpersTest(unittest.TestCase):
    def test_cacheable_ts_not_playlist(self) -> None:
        ts = "https://po.tudeporteshoy.xyz/espnpremium/tracks-v1a1/2026/08/14/08/53/04-03003.ts?token=abc"
        playlist = "https://po.tudeporteshoy.xyz/espnpremium/tracks-v1a1/mono.m3u8?token=abc"
        self.assertTrue(is_cacheable_segment(ts))
        self.assertFalse(is_cacheable_segment(playlist))

    def test_token_expiry_from_cdn_token(self) -> None:
        token = "f1e3f4239730f261a13b4c1e7c3e17fbf6073b8a-63-1786742594-1786688594"
        self.assertEqual(token_expiry_unix(token), 1786742594)

    def test_raw_query_keeps_token_bytes(self) -> None:
        url = "https://po.example/x.m3u8?token=ab+cd==&ip=1.2.3.4"
        self.assertEqual(raw_query_value(url, "token"), "ab+cd==")
        self.assertEqual(raw_query_value(url, "ip"), "1.2.3.4")

    def test_manifest_ttl_uses_token_timestamp(self) -> None:
        future = int(time.time()) + 600
        url = f"https://po.example/mono.m3u8?token=deadbeef-1-{future}-{future - 10}"
        expires = manifest_urls_expire_at([url], ttl_seconds=1800)
        self.assertLess(expires, time.time() + 600)
        self.assertGreater(expires, time.time() + 400)


class SegmentCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_hit_after_store(self) -> None:
        cache = SegmentCache(max_bytes=1024, ttl_seconds=30)

        async def loader():
            return "video/MP2T", b"mpegts-bytes"

        ctype, body, state = await cache.get_or_load("seg-a", loader)
        self.assertEqual(state, "MISS")
        self.assertEqual(body, b"mpegts-bytes")
        ctype2, body2, state2 = await cache.get_or_load("seg-a", loader)
        self.assertEqual(state2, "HIT")
        self.assertEqual(body2, body)
        self.assertEqual(ctype2, ctype)

    async def test_single_flight_one_loader(self) -> None:
        cache = SegmentCache(max_bytes=10_000, ttl_seconds=30)
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return "video/MP2T", b"shared"

        results = await asyncio.gather(
            cache.get_or_load("same", loader),
            cache.get_or_load("same", loader),
            cache.get_or_load("same", loader),
        )
        self.assertEqual(calls, 1)
        states = {item[2] for item in results}
        self.assertEqual(states, {"MISS", "HIT"})
        self.assertTrue(all(item[1] == b"shared" for item in results))

    async def test_lru_evicts_oldest(self) -> None:
        cache = SegmentCache(max_bytes=100, ttl_seconds=30)

        async def load_a():
            return "video/MP2T", b"a" * 60

        async def load_b():
            return "video/MP2T", b"b" * 60

        await cache.get_or_load("a", load_a)
        await cache.get_or_load("b", load_b)
        self.assertIsNone(await cache.get("a"))
        self.assertIsNotNone(await cache.get("b"))

    async def test_ttl_expires(self) -> None:
        cache = SegmentCache(max_bytes=1000, ttl_seconds=0.05)

        async def loader():
            return "video/MP2T", b"soon-gone"

        await cache.get_or_load("x", loader)
        await asyncio.sleep(0.08)
        self.assertIsNone(await cache.get("x"))


class TokenCacheTest(unittest.TestCase):
    def test_reuse_until_invalidate(self) -> None:
        resolver = HlsResolver(HlsResolverSettings(token_ttl_seconds=1800))
        key = resolver.token_cache_key("https://tudeporteshoy.xyz/embed/eventos.html?r=abc")
        url = "https://po.tudeporteshoy.xyz/espnpremium/tracks-v1a1/mono.m3u8?token=deadbeef-1-2000000000"
        resolver._token_cache_set(key, [url])
        cached = resolver._token_cache_get(key)
        self.assertEqual(cached, [url])
        resolver.invalidate_tokens(failed_url=url)
        self.assertIsNone(resolver._token_cache_get(key))

    def test_same_stream_shares_key(self) -> None:
        resolver = HlsResolver(HlsResolverSettings())
        embed = (
            "https://tudeporteshoy.xyz/embed/eventos.html"
            "?r=aHR0cHM6Ly9zdHJlYW10cC1nb2xkZW4xLmNsaWNrL2dsb2JhbDIucGhwP3N0cmVhbT1lc3BucHJlbWl1bQ=="
        )
        key = resolver.token_cache_key(embed)
        self.assertIn("global2.php", key)
        self.assertIn("espnpremium", key)


if __name__ == "__main__":
    unittest.main()
