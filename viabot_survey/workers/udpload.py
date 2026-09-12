"""Measure the link the way teleoperation actually uses it.

Everything else here measures an idle link. Ping sends sixty bytes a second and
asks how long they take; that finds a garage where nothing gets through. It does
not find the more interesting failure: a spot that looks fine empty and falls
apart the moment you put a video stream on it. Carriers shape UDP differently
from TCP, and drop it first under contention, so a link that passes a TCP speed
test can still be useless for teleop.

So this holds a single UDP stream open for the whole walk, at the bitrate a real
Formant session uses, and records the jitter and loss the receiving end sees
every second. That is the same question the operator in California is asking,
asked continuously, at every point in the garage.

Two deliberate choices about realism:

**Datagrams are RTP-sized, not iperf3's default.** iperf3 sends 32 KB UDP
datagrams unless told otherwise, which the IP layer then fragments into two
dozen packets. Lose any one and the whole datagram counts as lost, so the loss
figure comes out several times worse than what a real 1200-byte video packet
would see. The default would make every garage look terrible.

**The stream runs downlink.** ``-R`` has the server send and the rig receive,
which is the direction the video travels and the direction that matters.

This costs real cellular data — bitrate times the length of the walk, and
nothing else the rig does comes close — so it ships disabled and enforces a
per-run ceiling.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any

from .base import STATE_DEGRADED, STATE_FAILED, Worker

#: One per-second reading from the receiving end. The trailing "sender" and
#: "receiver" summary lines have the same shape, hence the end anchor.
#:
#:   [  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.062 ms  0/105 (0%)
INTERVAL_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<transfer>[\d.]+)\s+(?P<transfer_unit>[KMGT]?)Bytes\s+"
    r"(?P<rate>[\d.]+)\s+(?P<rate_unit>[KMGT]?)bits/sec\s+"
    r"(?P<jitter>[\d.]+)\s+ms\s+"
    r"(?P<lost>\d+)/(?P<total>\d+)\s+\((?P<loss_pct>[\d.]+)%\)\s*$"
)

_BYTE_SCALE = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_BIT_SCALE = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3, "T": 1e6}

#: Bytes per datagram. Real-time video is sent in packets that fit inside a
#: path MTU; 1200 is the figure WebRTC implementations settle on.
DEFAULT_DATAGRAM_BYTES = 1200

#: Readings older than this are not current any more — the stream has stalled
#: or died, which in a garage usually means the link has.
STALE_AFTER_S = 3.0


def parse_interval(line: str) -> dict[str, Any] | None:
    """Turn one iperf3 interval line into a reading, or None if it is not one."""
    match = INTERVAL_RE.match(line.strip())
    if not match or line.rstrip().endswith(("sender", "receiver")):
        return None
    fields = match.groupdict()
    return {
        "jitter_ms": float(fields["jitter"]),
        "loss_pct": float(fields["loss_pct"]),
        "lost": int(fields["lost"]),
        "total": int(fields["total"]),
        "mbps": float(fields["rate"]) * _BIT_SCALE[fields["rate_unit"]],
        "bytes": int(float(fields["transfer"]) * _BYTE_SCALE[fields["transfer_unit"]]),
        "seconds": float(fields["end"]) - float(fields["start"]),
    }


class UdpLoadWorker(Worker):
    """Streams jitter and loss under a constant UDP load."""

    name = "udp_load"

    def __init__(self, server: str | None, port: int = 5201,
                 bitrate: str = "1.5M", datagram_bytes: int = DEFAULT_DATAGRAM_BYTES,
                 direction: str = "download", interface: str | None = None,
                 run_data_budget_mb: float | None = 500.0,
                 username: str = "", password: str = "",
                 public_key_path: str = "",
                 enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled and bool(server), **kwargs)
        self.server = server
        self.port = int(port)
        self.bitrate = str(bitrate)
        self.datagram_bytes = int(datagram_bytes)
        self.direction = direction
        self.interface = interface
        self.run_data_budget_mb = run_data_budget_mb
        self.username = username
        self.password = password
        self.public_key_path = public_key_path
        self._proc: subprocess.Popen | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_ts: float = 0.0
        self._run_bytes = 0
        self._total_bytes = 0
        self._budget_spent = False
        self._last_line: str | None = None

    # -- command -------------------------------------------------------------

    def build_command(self) -> list[str]:
        cmd = [
            "iperf3", "-c", str(self.server), "-p", str(self.port),
            "-u",                      # UDP: the protocol teleop actually uses
            "-b", self.bitrate,
            "-l", str(self.datagram_bytes),
            "-t", "0",                 # run until stopped, not for a fixed test
            "-i", "1",                 # one reading a second, matching the sampler
            "--forceflush",            # or the readings arrive in a lump at the end
        ]
        if self.direction != "upload":
            # -R has the server send and the rig receive. Jitter and loss are
            # only reported by whichever end receives, and the receiving end we
            # care about is the one in the garage.
            cmd.append("-R")
        if self.interface:
            cmd += ["-B", self.interface]
        if self.username and self.public_key_path:
            # An iperf3 server reachable from the internet with no
            # authentication is a bandwidth allowance anyone can spend. The
            # password goes through the environment, not argv, so it is not in
            # ps for the length of a walk.
            cmd += ["--username", self.username,
                    "--rsa-public-key-path", self.public_key_path]
        return cmd

    def build_env(self) -> dict[str, str] | None:
        if not (self.username and self.password):
            return None
        return {**os.environ, "IPERF3_PASSWORD": self.password}

    # -- loop ----------------------------------------------------------------

    def run_once(self) -> None:
        if shutil.which("iperf3") is None:
            self._set_state(STATE_FAILED, "iperf3 is not installed")
            self.emit("error", "iperf3 binary not found; install iperf3")
            self.wait(60)
            return
        if self.budget_spent:
            # Nothing to do until the next run resets the allowance. Idle
            # rather than exit, so the worker is not restart-looping.
            self.wait(30)
            return

        self._proc = subprocess.Popen(
            self.build_command(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, errors="replace", bufsize=1, env=self.build_env())
        try:
            for line in self._proc.stdout:  # type: ignore[union-attr]
                if self.stopping:
                    return
                self._consume(line)
                if self.budget_spent:
                    self.emit("warning",
                              "UDP load test stopped: run data budget reached")
                    return
        finally:
            self._terminate()

    def _consume(self, line: str) -> None:
        self._last_line = line.strip() or self._last_line
        reading = parse_interval(line)
        if reading is None:
            return
        self._latest = reading
        self._latest_ts = time.time()
        self._run_bytes += reading["bytes"]
        self._total_bytes += reading["bytes"]

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    def on_stop(self) -> None:
        self._terminate()

    # -- budget --------------------------------------------------------------

    @property
    def budget_spent(self) -> bool:
        if self.run_data_budget_mb is None:
            return False
        return self._run_bytes >= float(self.run_data_budget_mb) * 1024 ** 2

    def begin_run(self) -> None:
        """A new walk gets a fresh data allowance."""
        self._run_bytes = 0

    # -- readings ------------------------------------------------------------

    def sample_fields(self) -> dict[str, Any]:
        """This second's reading, or nothing if the stream is not current.

        Stale readings are dropped rather than repeated. When the link dies the
        iperf3 control channel — which is TCP — drops with it and the stream
        ends, so a dead zone shows up here as *no reading* rather than as 100%
        loss. Carrying the last good number forward would paint a dead spot as
        healthy.
        """
        if self._latest is None or (time.time() - self._latest_ts) > STALE_AFTER_S:
            return {"udp_jitter_ms": None, "udp_loss_pct": None, "udp_mbps": None}
        return {
            "udp_jitter_ms": self._latest["jitter_ms"],
            "udp_loss_pct": self._latest["loss_pct"],
            "udp_mbps": round(self._latest["mbps"], 3),
        }

    def snapshot(self) -> dict[str, Any]:
        fields = self.sample_fields()
        return {
            "server": self.server,
            "bitrate": self.bitrate,
            "streaming": fields["udp_loss_pct"] is not None,
            "run_mb": round(self._run_bytes / 1024 ** 2, 1),
            "total_mb": round(self._total_bytes / 1024 ** 2, 1),
            "budget_mb": self.run_data_budget_mb,
            "budget_spent": self.budget_spent,
            "last_line": self._last_line,
            **fields,
        }
