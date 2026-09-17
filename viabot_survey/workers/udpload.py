"""Measure the link the way teleoperation actually uses it, in both directions.

Everything else here measures an idle link. Ping sends sixty bytes a second and
asks how long they take; that finds a garage where nothing gets through. It does
not find the more interesting failure: a spot that looks fine empty and falls
apart the moment you put a video stream on it. Carriers shape UDP differently
from TCP and drop it first under contention, so a link that passes a TCP speed
test can still be useless for teleop.

A real session is asymmetric, and both halves matter for different reasons:

**Uplink** carries the robot's video to the operator. It is the heavy stream,
and cellular uplink is typically the weaker direction, so this is usually what
decides whether a spot is workable. Measuring only downlink would flatter every
garage.

**Downlink** carries the operator's commands back. It is small, but if it
collapses the robot stops taking orders, which is its own kind of failure.

iperf3 only reports jitter and loss at the *receiving* end, which shapes how
each direction is measured. Downlink is received by the rig, so its numbers
arrive live, once a second. Uplink is received by the server, so it runs in
blocks and asks for the server's own per-second output at the end of each one.
The readings are then backfilled onto the samples they belong to. Nothing is
lost by the delay: the report is built when the run ends.

Two deliberate choices about realism:

**Datagrams are RTP-sized, not iperf3's default.** iperf3 sends 32 KB UDP
datagrams unless told otherwise, which the IP layer then fragments into two
dozen packets. Lose any one and the whole datagram counts as lost, so the loss
figure comes out several times worse than what a real 1200-byte video packet
would see. The default would make every garage look terrible.

**Each direction gets the bitrate that direction really carries.** Loading the
downlink with video-sized traffic would measure a session nobody runs.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from collections import deque
from typing import Any, Callable

from .base import STATE_DEGRADED, STATE_FAILED, Worker

log = logging.getLogger(__name__)

#: A per-second line from the receiving end, carrying jitter and loss:
#:
#:   [  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  0.062 ms  0/105 (0%)
RECEIVER_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<transfer>[\d.]+)\s+(?P<transfer_unit>[KMGT]?)Bytes\s+"
    r"(?P<rate>[\d.]+)\s+(?P<rate_unit>[KMGT]?)bits/sec\s+"
    r"(?P<jitter>[\d.]+)\s+ms\s+"
    r"(?P<lost>\d+)/(?P<total>\d+)\s+\((?P<loss_pct>[\d.]+)%\)\s*$"
)

#: The sending end's line has no jitter or loss — only what it put on the wire:
#:
#:   [  5]   0.00-1.00   sec   123 KBytes  1.01 Mbits/sec  105
SENDER_RE = re.compile(
    r"^\[\s*\d+\]\s+"
    r"(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec\s+"
    r"(?P<transfer>[\d.]+)\s+(?P<transfer_unit>[KMGT]?)Bytes\s+"
    r"(?P<rate>[\d.]+)\s+(?P<rate_unit>[KMGT]?)bits/sec\s+"
    r"(?P<total>\d+)\s*$"
)

_BYTE_SCALE = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_BIT_SCALE = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3, "T": 1e6}

#: Bytes per datagram. Real-time video is sent in packets that fit inside a path
#: MTU; 1200 is the figure WebRTC implementations settle on.
DEFAULT_DATAGRAM_BYTES = 1200

#: Readings older than this are not current any more.
STALE_AFTER_S = 3.0

UPLINK, DOWNLINK = "upload", "download"

#: Sample columns each direction fills.
FIELDS = {
    UPLINK: ("udp_up_jitter_ms", "udp_up_loss_pct", "udp_up_mbps"),
    DOWNLINK: ("udp_down_jitter_ms", "udp_down_loss_pct", "udp_down_mbps"),
}


#: iperf3 prints its failures on stderr, which is folded into stdout here, so
#: the reason a test produced nothing is already in hand — it just has to be
#: read rather than thrown away.
ERROR_RE = re.compile(r"^iperf3: error - (?P<message>.+?)\s*$", re.MULTILINE)

#: Measured against iperf3 3.16: a client clock 10 s out authenticates, 11 s
#: out is rejected, and the message is the same one a wrong password gets.
AUTH_SKEW_TOLERANCE_S = 10

AUTH_HINT = (
    "the server rejected the credentials. Either udp_load.username/password "
    f"do not match the server's, or the rig's clock is more than "
    f"{AUTH_SKEW_TOLERANCE_S}s out — iperf3 signs every test with a timestamp "
    "and this Pi has no RTC, so an unsynchronised clock fails exactly like a "
    "wrong password"
)


def parse_error(text: str) -> str | None:
    """The reason iperf3 gave for failing, in words worth showing an operator.

    Authorization failures get spelled out because the message iperf3 prints
    names neither of its two causes, and one of them — a clock the Pi cannot
    keep on its own — is not the one anybody checks first.
    """
    match = ERROR_RE.search(text)
    if not match:
        return None
    message = match.group("message")
    if "authorization" in message.lower():
        return AUTH_HINT
    return message


def address_of(text: str) -> str | None:
    """The IPv4 address out of `ip -o -4 addr show dev eth0` output.

        3: eth0    inet 192.168.1.42/24 brd ... scope global eth0

    Parsed rather than shelled out to in a test, so this can be checked
    without the interface existing.
    """
    for line in text.splitlines():
        parts = line.split()
        if "inet" in parts:
            candidate = parts[parts.index("inet") + 1].split("/")[0]
            if candidate:
                return candidate
    return None


def interface_address(interface: str) -> str | None:
    """What address to bind to, or None if the interface has none right now.

    iperf3's -B takes an *address*, unlike ping's -I which takes an interface
    name — passing it "eth0" fails with "Name or service not known" and the
    whole test never runs. The modem can also be between leases, in which case
    there is nothing to bind to and the routing table is a better answer than
    failing.
    """
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show", "dev", interface],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return address_of(out.stdout) if out.returncode == 0 else None


def parse_bitrate_mbps(value: Any) -> float | None:
    """iperf3's own bitrate spelling ("1.5M", "300k", "750000") as Mbit/s.

    The report needs the *offered* rate to draw against what arrived, and the
    configured string is the only place it is written down.
    """
    if value is None:
        return None
    text = str(value).strip().rstrip("Bb")
    match = re.fullmatch(r"([\d.]+)\s*([KMGT]?)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1)) * _BIT_SCALE[match.group(2).upper()]
    except (ValueError, KeyError):
        return None


def _scale(match: re.Match) -> dict[str, Any]:
    fields = match.groupdict()
    return {
        "mbps": float(fields["rate"]) * _BIT_SCALE[fields["rate_unit"]],
        "bytes": int(float(fields["transfer"]) * _BYTE_SCALE[fields["transfer_unit"]]),
        "start_s": float(fields["start"]),
        "end_s": float(fields["end"]),
    }


def parse_interval(line: str) -> dict[str, Any] | None:
    """One iperf3 interval line, from either end, or None if it is not one.

    The per-run summary lines have exactly the same shape as a per-second one
    and are distinguished only by a trailing word, so counting one would put a
    whole-run average into a single second.
    """
    line = line.strip()
    if line.endswith(("sender", "receiver")):
        return None
    match = RECEIVER_RE.match(line)
    if match:
        reading = _scale(match)
        reading.update(jitter_ms=float(match.group("jitter")),
                       loss_pct=float(match.group("loss_pct")),
                       lost=int(match.group("lost")),
                       total=int(match.group("total")))
        return reading
    match = SENDER_RE.match(line)
    if match:
        reading = _scale(match)
        reading.update(jitter_ms=None, loss_pct=None, lost=None,
                       total=int(match.group("total")))
        return reading
    return None


def parse_server_output(text: str) -> list[dict[str, Any]]:
    """The per-second receiver readings iperf3 relays back from the server.

    This is the only way to see uplink jitter and loss from the rig: the
    receiving end is the one that can measure them, and for uplink that end is
    in Dallas.
    """
    _, _, tail = text.partition("Server output:")
    if not tail:
        return []
    readings = []
    for line in tail.splitlines():
        reading = parse_interval(line)
        if reading and reading.get("loss_pct") is not None:
            readings.append(reading)
    return readings


class UdpLoadWorker(Worker):
    """One direction of the teleop load. Two of these make a session."""

    def __init__(self, server: str | None, direction: str = UPLINK,
                 port: int = 5201, bitrate: str = "2M",
                 datagram_bytes: int = DEFAULT_DATAGRAM_BYTES,
                 block_s: float = 30.0, interface: str | None = None,
                 run_data_budget_mb: float | None = 2000.0,
                 username: str = "", password: str = "", public_key_path: str = "",
                 on_backfill: Callable[[list[tuple[float, dict]]], None] | None = None,
                 enabled: bool = True, **kwargs: Any) -> None:
        super().__init__(enabled=enabled and bool(server), **kwargs)
        self.direction = direction
        self.server = server
        self.port = int(port)
        self.bitrate = str(bitrate)
        self.datagram_bytes = int(datagram_bytes)
        self.block_s = max(5.0, float(block_s))
        self.interface = interface
        self.run_data_budget_mb = run_data_budget_mb
        self.username = username
        self.password = password
        self.public_key_path = public_key_path
        self._on_backfill = on_backfill
        self._proc: subprocess.Popen | None = None
        self._latest: dict[str, Any] | None = None
        self._latest_ts: float = 0.0
        self._run_bytes = 0
        self._total_bytes = 0
        self._backfilled = 0
        self._last_line: str | None = None
        self._warned_no_address = False
        self._last_reason: str | None = None

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"udp_{'up' if self.direction == UPLINK else 'down'}"

    # -- command -------------------------------------------------------------

    def build_command(self) -> list[str]:
        cmd = [
            "iperf3", "-c", str(self.server), "-p", str(self.port),
            "-u",                             # UDP: what teleop actually uses
            "-b", self.bitrate,
            "-l", str(self.datagram_bytes),
            "-i", "1",                        # one reading a second
            "--forceflush",                   # or they arrive in a lump at the end
        ]
        if self.direction == DOWNLINK:
            # -R has the server send and the rig receive. The rig is then the
            # receiving end, so jitter and loss come back live.
            cmd += ["-R", "-t", "0"]          # -t 0: for the whole walk
        else:
            # Uplink: the rig sends, so only the server can see what arrived.
            # Run a block and ask it what it saw.
            cmd += ["-t", str(int(self.block_s)), "--get-server-output"]
        # The measured link is the modem's interface, not the Wi-Fi the phone
        # is on. Bind to it so a second route can never quietly send the test
        # somewhere else. If it has no address there is nothing to bind to, and
        # the routing table is a better answer than a test that will not start.
        if self.interface:
            address = interface_address(self.interface)
            if address:
                cmd += ["-B", address]
            elif not self._warned_no_address:
                self._warned_no_address = True
                log.warning("%s has no IPv4 address; letting the routing table "
                            "choose which interface the load test uses",
                            self.interface)
        if self.username and self.public_key_path:
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
            self.wait(30)
            return
        if self.direction == DOWNLINK:
            self._run_stream()
        else:
            self._run_block()

    def _launch(self) -> subprocess.Popen:
        self._proc = subprocess.Popen(
            self.build_command(), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, errors="replace", bufsize=1, env=self.build_env())
        return self._proc

    def _run_stream(self) -> None:
        """Downlink: one long-lived stream, read a line at a time."""
        proc = self._launch()
        # Enough of a tail to hold whatever iperf3 said on its way out. A
        # stream that never yields a reading has a reason, and it is here.
        tail: deque[str] = deque(maxlen=20)
        measured = False
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if self.stopping:
                    return
                tail.append(line)
                reading = self._consume(line)
                if reading and reading.get("loss_pct") is not None:
                    self._latest = reading
                    self._latest_ts = time.time()
                    measured = True
                if self.budget_spent:
                    self.emit("warning", f"{self.name}: run data budget reached")
                    return
            if not measured and not self.stopping:
                self._fail_with_reason("".join(tail), "the stream produced no readings")
        finally:
            self._terminate()

    def _fail_with_reason(self, text: str, fallback: str) -> None:
        """Report why a test produced nothing, saying it out loud once.

        Once, because uplink blocks restart every 30 seconds: a misconfigured
        credential would otherwise write the same line into the event log twice
        a minute for the length of a walk and bury everything else in it.
        """
        parsed = parse_error(text)
        reason = parsed or fallback
        # A reason iperf3 gave is a fault to fix; silence is merely a bad spot.
        self._set_state(STATE_FAILED if parsed else STATE_DEGRADED, reason)
        failed = parsed is not None
        if reason != self._last_reason:
            self._last_reason = reason
            self.emit("error" if failed else "warning", f"{self.name}: {reason}")

    def _run_block(self) -> None:
        """Uplink: a fixed block, then recover what the far end actually saw.

        The rig's own lines say only what it put on the wire. They are still
        worth reading as they arrive, because the wall-clock time each one
        lands is what lets the server's readings — which come back all at once
        at the end — be placed on the right seconds afterwards.
        """
        started = time.time()
        interval_ends: list[float] = []
        output: list[str] = []
        proc = self._launch()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                if self.stopping:
                    return
                output.append(line)
                reading = self._consume(line)
                if reading and reading.get("loss_pct") is None:
                    interval_ends.append(time.time())
                if self.budget_spent:
                    self.emit("warning", f"{self.name}: run data budget reached")
                    return
            proc.wait(timeout=10)
        finally:
            self._terminate()

        self._absorb_server_output("".join(output), started, interval_ends)

    def _absorb_server_output(self, text: str, started: float,
                              interval_ends: list[float]) -> None:
        readings = parse_server_output(text)
        if not readings:
            self._fail_with_reason(text, "the server returned no readings")
            return

        jitter, loss, mbps = FIELDS[UPLINK]
        backfill: list[tuple[float, dict]] = []
        for index, reading in enumerate(readings):
            # Prefer the wall clock recorded when the rig's own line for this
            # same second arrived: it absorbs the control-channel setup the
            # elapsed offsets do not know about. Fall back to the offsets when
            # the two ends disagree about how many seconds there were.
            if index < len(interval_ends):
                ts = interval_ends[index] - 0.5
            else:
                ts = started + (reading["start_s"] + reading["end_s"]) / 2
            backfill.append((ts, {
                jitter: reading["jitter_ms"],
                loss: reading["loss_pct"],
                mbps: round(reading["mbps"], 3),
            }))

        self._latest = readings[-1]
        self._latest_ts = time.time()
        self._backfilled += len(backfill)
        if self._on_backfill:
            self._on_backfill(backfill)

    def _consume(self, line: str) -> dict[str, Any] | None:
        self._last_line = line.strip() or self._last_line
        reading = parse_interval(line)
        if reading is None:
            return None
        self._run_bytes += reading["bytes"]
        self._total_bytes += reading["bytes"]
        return reading

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
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
        self._run_bytes = 0
        self._backfilled = 0

    # -- readings ------------------------------------------------------------

    def sample_fields(self) -> dict[str, Any]:
        """This second's reading, for the directions that can report live.

        Uplink cannot: its numbers only exist at the far end and arrive at the
        end of a block, so they are written onto their samples afterwards
        rather than through here.
        """
        jitter, loss, mbps = FIELDS[self.direction]
        if (self.direction == UPLINK or self._latest is None
                or self._latest.get("loss_pct") is None
                or (time.time() - self._latest_ts) > STALE_AFTER_S):
            return {jitter: None, loss: None, mbps: None}
        return {
            jitter: self._latest["jitter_ms"],
            loss: self._latest["loss_pct"],
            mbps: round(self._latest["mbps"], 3),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "server": self.server,
            "port": self.port,
            "bitrate": self.bitrate,
            "live": self.direction == DOWNLINK,
            "loss_pct": (self._latest or {}).get("loss_pct"),
            "jitter_ms": (self._latest or {}).get("jitter_ms"),
            "backfilled_samples": self._backfilled,
            "run_mb": round(self._run_bytes / 1024 ** 2, 1),
            "total_mb": round(self._total_bytes / 1024 ** 2, 1),
            "budget_mb": self.run_data_budget_mb,
            "budget_spent": self.budget_spent,
            "last_line": self._last_line,
        }
