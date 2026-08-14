"""In-memory concurrent-stream limiter keyed by subscriber sid + device."""

from __future__ import annotations

import asyncio
import logging
import re
import time

LOGGER = logging.getLogger("hls_proxy")

STREAM_LIMIT_DETAIL = "Límite de dispositivos alcanzado"
SID_REQUIRED_DETAIL = "Falta el identificador de sesión (sid)"
SID_PATTERN = re.compile(r"^[A-Za-z0-9._:+=@-]{1,128}$")


def sanitize_session_id(raw: str) -> str:
    value = (raw or "").strip()
    if not value or not SID_PATTERN.fullmatch(value):
        return ""
    return value


class StreamLimiter:
    """Cap distinct active devices per subscriber sid.

    A device stays active while it keeps requesting playlists/segments.
    Idle slots expire after idle_seconds (live HLS window).
    """

    def __init__(self, max_sessions: int, idle_seconds: float) -> None:
        self.max_sessions = max(0, int(max_sessions))
        self.idle_seconds = max(5.0, float(idle_seconds))
        self._sessions: dict[str, dict[str, float]] = {}
        self._lock = asyncio.Lock()
        self.admitted = 0
        self.refreshed = 0
        self.rejected = 0

    def enabled(self) -> bool:
        return self.max_sessions > 0

    def stats(self) -> dict[str, int]:
        now = time.time()
        active_sids = 0
        active_devices = 0
        for slots in self._sessions.values():
            live = sum(1 for seen in slots.values() if now - seen <= self.idle_seconds)
            if live:
                active_sids += 1
                active_devices += live
        return {
            "max_sessions": self.max_sessions,
            "idle_seconds": int(self.idle_seconds),
            "active_sids": active_sids,
            "active_devices": active_devices,
            "admitted": self.admitted,
            "refreshed": self.refreshed,
            "rejected": self.rejected,
        }

    def _purge_unlocked(self, slots: dict[str, float], now: float) -> None:
        expired = [key for key, seen in slots.items() if now - seen > self.idle_seconds]
        for key in expired:
            slots.pop(key, None)

    async def admit(self, sid: str, device_key: str) -> bool:
        """Return True if this device may stream. False = over the cap."""
        if not self.enabled():
            return True
        if not sid or not device_key:
            return False
        now = time.time()
        async with self._lock:
            slots = self._sessions.setdefault(sid, {})
            self._purge_unlocked(slots, now)
            if device_key in slots:
                slots[device_key] = now
                self.refreshed += 1
                return True
            if len(slots) >= self.max_sessions:
                self.rejected += 1
                LOGGER.warning(
                    "stream limit sid=%s device=%s active=%s max=%s",
                    sid[:48],
                    device_key[:48],
                    len(slots),
                    self.max_sessions,
                )
                return False
            slots[device_key] = now
            self.admitted += 1
            LOGGER.info(
                "stream admit sid=%s device=%s active=%s/%s",
                sid[:48],
                device_key[:48],
                len(slots),
                self.max_sessions,
            )
            return True
