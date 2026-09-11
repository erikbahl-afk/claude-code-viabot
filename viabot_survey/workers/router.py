"""Poll the router for modem signal statistics.

Failures here are expected and non-fatal — the router API is not characterised
yet — so the worker degrades quietly rather than spamming the event log.
"""

from __future__ import annotations

import time
from typing import Any

from ..router_client import RouterClient
from .base import STATE_DEGRADED, STATE_RUNNING, Worker


class RouterWorker(Worker):
    name = "router"

    #: Consecutive failures tolerated before the dashboard shows "degraded".
    FAILURE_GRACE = 3

    def __init__(self, client: RouterClient, interval_s: float = 2.0,
                 enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled and client.name != "null", **kwargs)
        self.client = client
        self.interval_s = max(1.0, float(interval_s))
        self._latest: dict[str, Any] = {}
        self._last_ok_ts: float | None = None
        self._last_error: str | None = None
        self._consecutive_failures = 0
        self._warned = False

    def run_once(self) -> None:
        while not self.stopping:
            self.poll_once()
            if not self.wait(self.interval_s):
                return

    def poll_once(self) -> None:
        try:
            fields = self.client.fetch()
        except Exception as exc:  # noqa: BLE001 - any transport error is tolerable
            self._consecutive_failures += 1
            self._last_error = str(exc)
            if self._consecutive_failures >= self.FAILURE_GRACE:
                self._set_state(STATE_DEGRADED, self._last_error)
                if not self._warned:
                    self._warned = True
                    self.emit("warning", f"router stats unavailable: {exc}")
            return

        self._consecutive_failures = 0
        self._last_error = None
        self._warned = False
        self._latest = fields
        self._last_ok_ts = time.time()
        self._set_state(STATE_RUNNING)

    def on_stop(self) -> None:
        self.client.close()

    def sample_fields(self) -> dict[str, Any]:
        """Just the columns the samples table stores."""
        return {key: self._latest.get(key)
                for key in ("rsrp", "rsrq", "sinr", "rssi", "band", "cell_id", "tech")}

    def snapshot(self) -> dict[str, Any]:
        return {
            "client": self.client.name,
            "last_ok_ts": self._last_ok_ts,
            "last_error": self._last_error,
            **self._latest,
        }
