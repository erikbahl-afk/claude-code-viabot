"""Periodic DNS resolution check.

Catches the failure mode where ICMP still flows but nothing usable works —
common at the edge of coverage, where the modem holds a registration but the
session is effectively dead. Costs a few hundred bytes a minute.
"""

from __future__ import annotations

import socket
import time
from typing import Any

from .base import Worker


class DnsWorker(Worker):
    name = "dns"

    def __init__(self, hostname: str = "google.com", interval_s: float = 30.0,
                 timeout_s: float = 5.0, enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled, **kwargs)
        self.hostname = hostname
        self.interval_s = max(5.0, float(interval_s))
        self.timeout_s = float(timeout_s)
        self._last_ms: float | None = None
        self._last_ok: bool | None = None
        self._last_error: str | None = None
        self._last_ts: float | None = None

    def run_once(self) -> None:
        while not self.stopping:
            self.resolve_once()
            if not self.wait(self.interval_s):
                return

    def resolve_once(self) -> None:
        started = time.monotonic()
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout_s)
        try:
            socket.getaddrinfo(self.hostname, 443, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            self._last_ok = False
            self._last_ms = None
            self._last_error = str(exc)
        else:
            self._last_ok = True
            self._last_ms = round((time.monotonic() - started) * 1000, 1)
            self._last_error = None
        finally:
            socket.setdefaulttimeout(previous_timeout)
            self._last_ts = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "ok": self._last_ok,
            "resolve_ms": self._last_ms,
            "last_error": self._last_error,
            "last_ts": self._last_ts,
        }
