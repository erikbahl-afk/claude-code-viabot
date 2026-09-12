"""iperf3 throughput testing.

Disabled out of the box: it needs an iperf3 server you control, and it is the
only part of the rig that consumes meaningful cellular data. Set
``iperf3.server`` and ``iperf3.enabled`` in config/config.yaml to turn it on.
See docs/IPERF_SERVER.md.

Two guards keep a runaway test from eating a SIM plan:

* ``bitrate`` caps each test (a capped test still finds dead zones — you learn
  where the link cannot even sustain the cap);
* ``run_data_budget_mb`` stops launching tests once a single run has moved that
  much data.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from typing import Any, Callable

from .base import STATE_DEGRADED, STATE_FAILED, Worker


class Iperf3Worker(Worker):
    name = "iperf3"

    def __init__(self, server: str | None, port: int = 5201, duration_s: int = 5,
                 interval_s: float = 60.0, bitrate: str | None = "25M",
                 direction: str = "download", streams: int = 1,
                 run_data_budget_mb: float | None = 2000,
                 interface: str | None = None, enabled: bool = False,
                 username: str = "", password: str = "", public_key_path: str = "",
                 on_result: Callable[[dict], None] | None = None, **kwargs: Any) -> None:
        super().__init__(enabled=enabled and bool(server), **kwargs)
        self.server = server
        self.port = int(port)
        self.duration_s = max(1, int(duration_s))
        self.interval_s = max(5.0, float(interval_s))
        self.bitrate = bitrate
        self.direction = direction
        self.streams = max(1, int(streams))
        self.run_data_budget_mb = run_data_budget_mb
        self.interface = interface
        self.username = username
        self.password = password
        self.public_key_path = public_key_path
        self._on_result = on_result
        self._proc: subprocess.Popen | None = None
        self._last: dict | None = None
        self._bytes_this_run = 0
        self._budget_exhausted = False
        self._manual = False

    # -- run accounting ------------------------------------------------------

    def reset_run_budget(self) -> None:
        self._bytes_this_run = 0
        self._budget_exhausted = False

    @property
    def budget_remaining_mb(self) -> float | None:
        if self.run_data_budget_mb is None:
            return None
        return round(self.run_data_budget_mb - self._bytes_this_run / 1e6, 1)

    def request_manual_test(self) -> None:
        """Ask for one test now, bypassing the interval (not the budget)."""
        self._manual = True

    # -- worker loop ---------------------------------------------------------

    def run_once(self) -> None:
        if shutil.which("iperf3") is None:
            self._set_state(STATE_FAILED, "iperf3 not installed (apt install iperf3)")
            self.emit("error", "iperf3 binary not found; install iperf3")
            self.wait(60)
            return

        next_test = time.monotonic()
        while not self.stopping:
            due = time.monotonic() >= next_test
            if self._manual or due:
                self._manual = False
                next_test = time.monotonic() + self.interval_s
                if self._budget_allows():
                    result = self.measure()
                    self._last = result
                    if self._on_result:
                        self._on_result(result)
            if not self.wait(0.5):
                return

    def _budget_allows(self) -> bool:
        if self.run_data_budget_mb is None:
            return True
        if self._bytes_this_run / 1e6 < self.run_data_budget_mb:
            return True
        if not self._budget_exhausted:
            self._budget_exhausted = True
            self._set_state(STATE_DEGRADED, "run data budget exhausted")
            self.emit("warning",
                      f"iperf3 paused: run used {self._bytes_this_run / 1e6:.0f} MB, "
                      f"budget {self.run_data_budget_mb} MB")
        return False

    def on_stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()

    # -- measurement ---------------------------------------------------------

    def build_command(self, reverse: bool) -> list[str]:
        cmd = ["iperf3", "-c", str(self.server), "-p", str(self.port),
               "-t", str(self.duration_s), "-P", str(self.streams), "-J"]
        if self.bitrate:
            cmd += ["-b", str(self.bitrate)]
        if reverse:
            # -R makes the server send, i.e. measures the downlink.
            cmd.append("-R")
        if self.interface:
            cmd += ["-B", self.interface] if _looks_like_ip(self.interface) else []
        if self.username and self.public_key_path:
            # Same shared server as the UDP load test, and the same reason: an
            # unauthenticated iperf3 server on the internet is free bandwidth
            # for whoever finds the port.
            cmd += ["--username", self.username,
                    "--rsa-public-key-path", self.public_key_path]
        return cmd

    def build_env(self) -> dict[str, str] | None:
        if not (self.username and self.password):
            return None
        return {**os.environ, "IPERF3_PASSWORD": self.password}

    def measure(self) -> dict:
        result: dict[str, Any] = {"ts": time.time(), "server": self.server,
                                  "down_mbps": None, "up_mbps": None,
                                  "retransmits": None, "bytes_used": 0, "error": None}
        directions: list[tuple[str, bool]] = []
        if self.direction in ("download", "both"):
            directions.append(("down_mbps", True))
        if self.direction in ("upload", "both"):
            directions.append(("up_mbps", False))

        for key, reverse in directions:
            parsed = self._run_iperf(reverse)
            if parsed.get("error"):
                result["error"] = parsed["error"]
                continue
            result[key] = parsed["mbps"]
            result["bytes_used"] += parsed["bytes"]
            if parsed.get("retransmits") is not None:
                result["retransmits"] = parsed["retransmits"]

        self._bytes_this_run += result["bytes_used"]
        return result

    def _run_iperf(self, reverse: bool) -> dict:
        # Give iperf3 headroom over the nominal duration before we pull the plug.
        timeout = self.duration_s + 20
        try:
            self._proc = subprocess.Popen(
                self.build_command(reverse), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=self.build_env())
            stdout, stderr = self._proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if self._proc:
                self._proc.kill()
                self._proc.communicate()
            return {"error": f"iperf3 timed out after {timeout}s"}
        except OSError as exc:
            return {"error": str(exc)}
        finally:
            self._proc = None

        return parse_iperf3_json(stdout, stderr)


def _looks_like_ip(value: str) -> bool:
    return value.replace(".", "").isdigit()


def parse_iperf3_json(stdout: str, stderr: str = "") -> dict:
    """Extract Mbit/s and bytes moved from iperf3 ``-J`` output."""
    if not stdout.strip():
        return {"error": (stderr or "iperf3 produced no output").strip()}
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": (stderr or stdout)[:300].strip()}

    if data.get("error"):
        return {"error": str(data["error"])}

    end = data.get("end", {})
    # For a reverse test the meaningful figure is what the receiver actually
    # got; sum_sent flatters the result when the network drops packets.
    summary = end.get("sum_received") or end.get("sum_sent") or {}
    bits = summary.get("bits_per_second")
    if bits is None:
        return {"error": "iperf3 output missing throughput summary"}

    retransmits = (end.get("sum_sent") or {}).get("retransmits")
    return {
        "mbps": round(bits / 1e6, 2),
        "bytes": int(summary.get("bytes", 0)),
        "retransmits": retransmits,
        "error": None,
    }
