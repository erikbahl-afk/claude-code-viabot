"""Continuous ICMP ping — the rig's primary dead-zone signal.

We shell out to iputils ``ping`` rather than opening raw sockets: it needs no
elevated privileges on Raspberry Pi OS, and ``-O`` gives us an explicit
"no answer yet" line per lost packet, so loss shows up in the UI the moment it
happens instead of only when a summary is printed.

Bandwidth cost is negligible (~64 bytes/s), so this runs for the whole walk.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections import OrderedDict
from typing import Any

from .base import STATE_DEGRADED, STATE_FAILED, STATE_RUNNING, Worker

# [1755302400.123456] 64 bytes from 8.8.8.8: icmp_seq=3 ttl=118 time=48.2 ms
REPLY_RE = re.compile(r"icmp_seq=(\d+).*?\btime=([\d.]+)\s*ms")
# [1755302402.130000] no answer yet for icmp_seq=3
NO_ANSWER_RE = re.compile(r"no answer yet for icmp_seq=(\d+)")
# [1755302402.130000] From 192.168.1.1 icmp_seq=3 Destination Host Unreachable
UNREACHABLE_RE = re.compile(r"icmp_seq=(\d+).*?(Unreachable|Time to live exceeded)")
TIMESTAMP_RE = re.compile(r"^\[(\d+\.\d+)\]\s*")

# Fatal setup errors worth surfacing verbatim rather than silently retrying.
FATAL_MARKERS = (
    "Name or service not known",
    "unknown host",
    "Cannot assign requested address",
    "SO_BINDTODEVICE",
    "Operation not permitted",
)


class PingWorker(Worker):
    """Streams ping output into a rolling window of per-sequence results."""

    name = "ping"

    def __init__(self, target: str, interval_s: float = 1.0, timeout_s: float = 2.0,
                 window_s: float = 10.0, interface: str | None = None,
                 enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled, **kwargs)
        self.target = target
        self.interval_s = max(0.2, float(interval_s))
        self.timeout_s = float(timeout_s)
        self.window_s = float(window_s)
        self.interface = interface
        self._proc: subprocess.Popen | None = None
        # seq -> {"ts": float, "rtt": float | None}. Ordered so pruning is cheap.
        self._results: OrderedDict[int, dict] = OrderedDict()
        self._last_reply_ts: float | None = None
        self._last_line: str | None = None

    # -- command -------------------------------------------------------------

    def build_command(self) -> list[str]:
        cmd = [
            "ping",
            "-n",                        # no reverse DNS, keeps output prompt
            "-O",                        # report each unanswered packet
            "-D",                        # prefix every line with a unix timestamp
            "-i", str(self.interval_s),
            "-W", str(self.timeout_s),
        ]
        if self.interface:
            # Pin to the wired uplink. Without this, a future routing change
            # could silently measure the wrong interface.
            cmd += ["-I", self.interface]
        cmd.append(self.target)
        return cmd

    # -- worker loop ---------------------------------------------------------

    def run_once(self) -> None:
        if shutil.which("ping") is None:
            self._set_state(STATE_FAILED, "ping not installed (apt install iputils-ping)")
            self.emit("error", "ping binary not found; install iputils-ping")
            self.wait(30)
            return

        self._proc = subprocess.Popen(
            self.build_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                if self.stopping:
                    break
                self.handle_line(line.rstrip("\n"))
        finally:
            self._terminate()

    def on_stop(self) -> None:
        self._terminate()

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()

    # -- parsing -------------------------------------------------------------

    def handle_line(self, line: str) -> None:
        """Fold one line of ping output into the rolling window."""
        if not line.strip():
            return
        self._last_line = line

        ts_match = TIMESTAMP_RE.match(line)
        ts = float(ts_match.group(1)) if ts_match else time.time()
        body = line[ts_match.end():] if ts_match else line

        for marker in FATAL_MARKERS:
            if marker in body:
                self._set_state(STATE_FAILED, body.strip())
                self.emit("error", f"ping: {body.strip()}")
                return

        reply = REPLY_RE.search(body)
        if reply:
            seq, rtt = int(reply.group(1)), float(reply.group(2))
            # A late reply supersedes an earlier "no answer yet" for that seq.
            self._record(seq, ts, rtt)
            self._last_reply_ts = ts
            return

        missed = NO_ANSWER_RE.search(body) or UNREACHABLE_RE.search(body)
        if missed:
            seq = int(missed.group(1))
            # Only register a loss if we have not already seen a reply; ping
            # can print the unreachable notice after a successful answer.
            if self._results.get(seq, {}).get("rtt") is None:
                self._record(seq, ts, None)

    def _record(self, seq: int, ts: float, rtt: float | None) -> None:
        self._results[seq] = {"ts": ts, "rtt": rtt}
        self._results.move_to_end(seq)
        self._prune(ts)

    def _prune(self, now: float) -> None:
        # Keep a generous multiple of the window so late replies still land.
        cutoff = now - max(self.window_s * 3, 60.0)
        while self._results:
            seq, entry = next(iter(self._results.items()))
            if entry["ts"] >= cutoff:
                break
            del self._results[seq]

    # -- readings ------------------------------------------------------------

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        cutoff = now - self.window_s
        window = [e for e in self._results.values() if e["ts"] >= cutoff]
        rtts = [e["rtt"] for e in window if e["rtt"] is not None]

        loss_pct: float | None = None
        if window:
            loss_pct = round(100.0 * (len(window) - len(rtts)) / len(window), 1)

        jitter = None
        if len(rtts) >= 2:
            deltas = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
            jitter = round(sum(deltas) / len(deltas), 2)

        latest = next((e["rtt"] for e in reversed(window) if e["rtt"] is not None), None)

        # If nothing has arrived at all for several intervals the link is down,
        # not merely quiet — say so rather than reporting a stale RTT.
        silent_for = None if self._last_reply_ts is None else round(now - self._last_reply_ts, 1)
        if silent_for is not None and silent_for > max(5 * self.interval_s, 5.0):
            latest = None
            loss_pct = 100.0
            if self.state not in (STATE_FAILED,):
                self._set_state(STATE_DEGRADED, f"no reply for {silent_for:.0f}s")
        elif silent_for is not None and self.state == STATE_DEGRADED:
            # Replies are flowing again, so say so. Nothing else clears this:
            # the base class sets RUNNING once before run_once(), and ping's
            # run_once() streams for the life of the worker and never returns.
            # Without this the health chip latches on the first dead zone and
            # reads "link problem" for the rest of the walk — long after the
            # link came back — which trains the operator to ignore it.
            self._set_state(STATE_RUNNING)

        return {
            "target": self.target,
            "rtt_ms": None if latest is None else round(latest, 2),
            "avg_rtt_ms": round(sum(rtts) / len(rtts), 2) if rtts else None,
            "loss_pct": loss_pct,
            "jitter_ms": jitter,
            "sent": len(window),
            "received": len(rtts),
            "silent_for_s": silent_for,
            "last_line": self._last_line,
        }
