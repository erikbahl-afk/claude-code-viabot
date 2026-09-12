"""Common scaffolding for the background measurement workers.

Every worker runs on its own daemon thread and exposes two things to the rest
of the app: :meth:`snapshot`, a plain dict of its current readings, and
:attr:`state`, a coarse health string the dashboard surfaces. Workers restart
themselves on unexpected failure with a capped backoff so that, for example,
unplugging the camera mid-walk does not silently end the survey.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

# Worker health, in increasing order of badness.
STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_DEGRADED = "degraded"
STATE_FAILED = "failed"
STATE_DISABLED = "disabled"


class Worker:
    """Base class: subclasses implement :meth:`run_once`."""

    name = "worker"
    #: Seconds to wait after a crash before the first restart attempt.
    restart_delay_s = 2.0
    max_restart_delay_s = 60.0

    def __init__(self, enabled: bool = True,
                 on_event: Callable[[str, str], None] | None = None) -> None:
        self.enabled = enabled
        self._on_event = on_event
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state = STATE_DISABLED if not enabled else STATE_STOPPED
        self._error: str | None = None
        self.restarts = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._set_state(STATE_STARTING)
        self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self.on_stop()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        self._set_state(STATE_DISABLED if not self.enabled else STATE_STOPPED)

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def wait(self, seconds: float) -> bool:
        """Sleep, waking early on stop. Returns True if we should keep going."""
        return not self._stop.wait(seconds)

    def _loop(self) -> None:
        delay = self.restart_delay_s
        while not self._stop.is_set():
            try:
                self._set_state(STATE_RUNNING)
                self.run_once()
                if self._stop.is_set():
                    break
                # run_once returning on its own is not an error, but it does
                # mean the underlying process ended; fall through to restart.
                self.emit("warning", f"{self.name} exited, restarting")
            except Exception as exc:  # noqa: BLE001 - a worker must never kill the app
                log.exception("%s crashed", self.name)
                self._set_state(STATE_FAILED, str(exc))
                self.emit("error", f"{self.name} crashed: {exc}")
            if self._stop.is_set():
                break
            self.restarts += 1
            if not self.wait(delay):
                break
            delay = min(delay * 2, self.max_restart_delay_s)
        self._set_state(STATE_DISABLED if not self.enabled else STATE_STOPPED)

    # -- hooks for subclasses ------------------------------------------------

    def run_once(self) -> None:
        raise NotImplementedError

    def on_stop(self) -> None:
        """Called on stop() before joining: tear down subprocesses here."""

    def snapshot(self) -> dict[str, Any]:
        return {}

    # -- shared helpers ------------------------------------------------------

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._lock:
            self._state = state
            self._error = error

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def emit(self, level: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(level, message)
            except Exception:  # noqa: BLE001
                log.exception("event callback failed for %s", self.name)

    def status(self) -> dict[str, Any]:
        """Worker health, plus whatever the subclass wants to report.

        The health fields are written last and win. A snapshot is partly
        built from data the worker did not author — the router's, for one,
        comes out of a modem — and a reading that happened to carry a key
        called "state" once replaced this worker's health with the modem's
        idea of its own connection. Everything downstream reads these five
        names to decide whether the rig is working.
        """
        return {
            **self.snapshot(),
            "name": self.name,
            "enabled": self.enabled,
            "state": self.state,
            "error": self.error,
            "restarts": self.restarts,
        }


def monotonic_sleep_until(deadline: float) -> float:
    """Return seconds remaining until ``deadline`` on the monotonic clock."""
    return max(0.0, deadline - time.monotonic())
