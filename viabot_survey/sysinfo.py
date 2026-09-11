"""Host facts the dashboard shows: clock sync, interfaces, disk, temperature.

Everything here degrades to ``None`` rather than raising — the dashboard must
render on a half-broken rig, which is exactly when you need to look at it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


def _run(cmd: list[str], timeout: float = 4.0) -> str | None:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def hostname() -> str:
    return socket.gethostname()


def uptime_s() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def clock_synced() -> bool | None:
    """Whether NTP has actually disciplined the clock.

    This matters more than it looks: the Pi has no real-time clock, so every
    timestamp — and therefore every video correlation — depends on it having
    reached a time server through the cellular link after boot.
    """
    output = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    if output is not None:
        return output.strip() == "yes"
    output = _run(["timedatectl", "status"])
    if output is not None:
        for line in output.splitlines():
            if "synchronized:" in line.lower():
                return line.strip().lower().endswith("yes")
    return None


def interface_address(name: str) -> str | None:
    output = _run(["ip", "-4", "-o", "addr", "show", "dev", name])
    if not output:
        return None
    for line in output.splitlines():
        parts = line.split()
        if "inet" in parts:
            return parts[parts.index("inet") + 1].split("/")[0]
    return None


def interface_up(name: str) -> bool | None:
    try:
        return Path(f"/sys/class/net/{name}/operstate").read_text().strip() == "up"
    except OSError:
        return None


def default_route_interface() -> str | None:
    output = _run(["ip", "route", "show", "default"])
    if not output:
        return None
    parts = output.split()
    if "dev" in parts:
        return parts[parts.index("dev") + 1]
    return None


def cpu_temperature_c() -> float | None:
    try:
        milli = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
    except (OSError, ValueError):
        return None
    return round(milli / 1000.0, 1)


def load_average() -> tuple[float, float, float] | None:
    try:
        parts = Path("/proc/loadavg").read_text().split()[:3]
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except (OSError, ValueError):
        return None


def disk_usage(path: Path) -> dict[str, float]:
    usage = shutil.disk_usage(path)
    return {
        "total_mb": round(usage.total / 1e6, 1),
        "free_mb": round(usage.free / 1e6, 1),
        "used_pct": round(100.0 * usage.used / usage.total, 1) if usage.total else 0.0,
    }


def wifi_clients(interface: str = "wlan0") -> int | None:
    """Count stations associated with the access point."""
    output = _run(["iw", "dev", interface, "station", "dump"])
    if output is None:
        return None
    return sum(1 for line in output.splitlines() if line.startswith("Station "))


def collect(uplink_interface: str = "eth0", ap_interface: str = "wlan0",
            data_dir: Path | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "hostname": hostname(),
        "now": time.time(),
        "timezone": time.strftime("%Z"),
        "uptime_s": uptime_s(),
        "clock_synced": clock_synced(),
        "cpu_temp_c": cpu_temperature_c(),
        "load_average": load_average(),
        "default_route_dev": default_route_interface(),
        "uplink": {
            "interface": uplink_interface,
            "up": interface_up(uplink_interface),
            "address": interface_address(uplink_interface),
        },
        "ap": {
            "interface": ap_interface,
            "up": interface_up(ap_interface),
            "address": interface_address(ap_interface),
            "clients": wifi_clients(ap_interface),
        },
    }
    if data_dir is not None:
        info["disk"] = disk_usage(data_dir)
    return info
