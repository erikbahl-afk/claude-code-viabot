"""Read the one-off capacity test's results and say what they mean.

Everything else on this rig measures a link carrying a *fixed* teleop-sized
load and asks whether it kept up. That answers "would a session like the one
we measured work here" and cannot answer "how much could this link carry if
asked" — which is the question the moment anyone suggests teleop needs a
heavier stream than the one we measured.

So `scripts/capacity_test.sh` saturates the link deliberately, once, outside a
walk, and this turns the JSON it leaves behind into a verdict. Kept separate
from the shell so the arithmetic can be tested without a rig, a server or a
cellular modem.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .workers.udpload import parse_server_output

_SCALE = {"": 1e-6, "K": 1e-3, "M": 1.0, "G": 1e3}


def parse_rate_mbps(value: Any) -> float | None:
    """"5M", "300k", "750000" as Mbit/s. Same spelling iperf3 takes."""
    if value is None:
        return None
    text = str(value).strip().rstrip("Bb")
    match = re.fullmatch(r"([\d.]+)\s*([KMG]?)", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1)) * _SCALE[match.group(2).upper()]
    except (ValueError, KeyError):
        return None


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def tcp_result(doc: dict | None) -> dict[str, Any]:
    """What a TCP test found, or why it found nothing.

    ``sum_received`` is the honest figure: it is what arrived at the far end,
    where ``sum_sent`` counts bytes that were put on the wire and may have been
    retransmitted.
    """
    if not doc:
        return {"error": "no result"}
    if doc.get("error"):
        return {"error": str(doc["error"])}
    end = doc.get("end") or {}
    received = end.get("sum_received") or {}
    sent = end.get("sum_sent") or {}
    bits = received.get("bits_per_second") or sent.get("bits_per_second")
    if not bits:
        return {"error": "the test produced no throughput figure"}
    return {
        "mbps": bits / 1e6,
        "retransmits": sent.get("retransmits"),
        "seconds": received.get("seconds") or sent.get("seconds"),
    }


def udp_result(doc: dict | None) -> dict[str, Any]:
    """What a constant-rate UDP test delivered, according to the end that knows.

    For an uplink test that end is the server, and its numbers only come back
    if the end-of-test exchange survives — which, on a flooded uplink, it does
    not. iperf3 then reports what the client *sent*, with no losses recorded.
    Every uplink figure this produced read as exactly the offered rate with
    exactly 0.0% loss, including 12 Mbit/s "delivered" over a link the same run
    had just measured at 1.64 Mbit/s.

    So the server's own per-second log is parsed instead, the same way the
    survey's load worker does it, and ``end.sum`` is used only when there is no
    server output at all — flagged, because it cannot be trusted on uplink.
    """
    if not doc:
        return {"error": "no result"}
    if doc.get("error"):
        return {"error": str(doc["error"])}

    readings = parse_server_output(doc.get("server_output_text") or "")
    if readings:
        rates = [r["mbps"] for r in readings]
        losses = [r["loss_pct"] for r in readings if r["loss_pct"] is not None]
        jitters = [r["jitter_ms"] for r in readings if r["jitter_ms"] is not None]
        return {
            "mbps": sum(rates) / len(rates),
            "loss_pct": sum(losses) / len(losses) if losses else None,
            "jitter_ms": sum(jitters) / len(jitters) if jitters else None,
            "seconds": len(readings),
        }

    summary = (doc.get("end") or {}).get("sum") or {}
    if summary.get("bits_per_second") is not None and not doc.get("_receiving"):
        return {"mbps": summary["bits_per_second"] / 1e6,
                "loss_pct": summary.get("lost_percent"),
                "jitter_ms": summary.get("jitter_ms"),
                "unverified": True}
    bits = summary.get("bits_per_second")
    if bits is None:
        return {"error": "the test produced no throughput figure"}
    return {
        "mbps": bits / 1e6,
        "loss_pct": summary.get("lost_percent"),
        "jitter_ms": summary.get("jitter_ms"),
    }


#: Measured against iperf3 3.16: a client clock 10 s out authenticates, 11 s
#: out is rejected — with the same message a wrong password gets.
AUTH_SKEW_TOLERANCE_S = 10


def auth_advice(skew_s: float | None) -> list[str]:
    """What to check when the server rejected the credentials.

    iperf3 signs every test with a timestamp, so an unsynchronised clock fails
    exactly like a wrong password. This Pi has no RTC, which makes the clock
    the more likely of the two and the one nobody checks first — so when the
    skew is known and out of bounds, say so instead of listing both.
    """
    if skew_s is not None and abs(skew_s) > AUTH_SKEW_TOLERANCE_S:
        return [
            f"The rig's clock is {skew_s:+.0f}s against the server, and iperf3",
            f"rejects anything more than {AUTH_SKEW_TOLERANCE_S}s out. That alone",
            "explains this. Fix the clock first:  sudo systemctl restart systemd-timesyncd",
            "This Pi has no RTC, so it starts every boot with no idea of the time.",
        ]
    lines = ["The server rejected the credentials. Three things do that:",
             "  1. udp_load.username / password not matching the server's",
             f"  2. the rig's clock being more than {AUTH_SKEW_TOLERANCE_S}s out —",
             "     iperf3 signs each test with a timestamp, and this Pi has no RTC",
             "  3. an iperf3 version mismatch. 3.17 changed the credential",
             "     encryption from PKCS#1 to OAEP and the two do not talk; the",
             "     server reports it as an authorization failure either way, and",
             "     only its own log says 'padding check failed'. The check above",
             "     tries both when this iperf3 is 3.17 or newer — compare",
             "     'iperf3 --version' on both machines."]
    if skew_s is not None:
        lines.append(f"  (clock checked: {skew_s:+.0f}s against the server, "
                     "so it is not that)")
    else:
        lines.append("  (the clock could not be checked against the server)")
    return lines


def headroom(ceiling_mbps: float | None, wanted_mbps: float | None) -> str:
    """How a measured ceiling reads against a rate somebody wants to send."""
    if not ceiling_mbps or not wanted_mbps:
        return ""
    ratio = ceiling_mbps / wanted_mbps
    if ratio >= 2:
        return f"{ratio:.1f}x headroom over {wanted_mbps:g} Mbit/s"
    if ratio >= 1:
        return (f"only {ratio:.1f}x over {wanted_mbps:g} Mbit/s — enough here, "
                "with nothing spare")
    return (f"BELOW {wanted_mbps:g} Mbit/s — this link cannot carry that "
            "stream at this spot")


def _line(label: str, result: dict, *, udp: bool = False) -> str:
    if result.get("error"):
        return f"  {label:9s} —  {result['error']}"
    out = f"  {label:9s} {result['mbps']:6.2f} Mbit/s"
    if result.get("unverified"):
        # The sender's own count. On a flooded uplink it reads as exactly the
        # rate that was offered, which is not a measurement of anything.
        return out + "   (what was SENT — the far end never reported back)"
    if udp:
        loss, jitter = result.get("loss_pct"), result.get("jitter_ms")
        if loss is not None:
            out += f"   loss {loss:5.1f}%"
        if jitter is not None:
            out += f"   jitter {jitter:5.1f} ms"
    elif result.get("retransmits"):
        out += f"   ({result['retransmits']} retransmits)"
    return out


def render(work: Path, *, configured_up: str | None, configured_down: str | None,
           udp_rate: str | None, skew_s: float | None = None) -> str:
    up = tcp_result(load(work / "up.json"))
    down = tcp_result(load(work / "down.json"))

    lines = ["", "Ceiling at this spot (TCP, uncapped):",
             _line("uplink", up), _line("downlink", down), ""]

    if any("authorization" in (r.get("error") or "") for r in (up, down)):
        lines += auth_advice(skew_s) + [""]

    wanted_up = parse_rate_mbps(configured_up)
    note = headroom(up.get("mbps"), wanted_up)
    if note:
        lines.append(f"The survey currently loads the uplink at "
                     f"{wanted_up:g} Mbit/s — {note}.")
    wanted_down = parse_rate_mbps(configured_down)
    note = headroom(down.get("mbps"), wanted_down)
    if note:
        lines.append(f"Downlink is loaded at {wanted_down:g} Mbit/s — {note}.")

    if udp_rate:
        offered = parse_rate_mbps(udp_rate)
        lines += ["", f"Constant {udp_rate} both ways, the way teleop behaves:",
                  _line("uplink", udp_result(load(work / "uup.json")), udp=True),
                  _line("downlink", udp_result(load(work / "udown.json")), udp=True)]
        uu = udp_result(load(work / "uup.json"))
        if offered and not uu.get("error") and uu.get("loss_pct") is not None:
            verdict = ("carries it" if uu["loss_pct"] < 1
                       else "struggles with it" if uu["loss_pct"] < 5
                       else "cannot carry it")
            lines.append(f"  → this spot {verdict} on the uplink.")

    lines += [
        "",
        "This is one spot, not a garage. Signal at a loading bay says nothing",
        "about level 3, and the ceiling moves with how busy the cell is. Take",
        "it as an order of magnitude, and re-run it somewhere weak.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m viabot_survey.capacity <results-dir>",
              file=sys.stderr)
        return 2
    raw = os.environ.get("CLOCK_SKEW_S") or ""
    try:
        skew: float | None = float(raw)
    except ValueError:
        skew = None
    print(render(Path(argv[1]),
                 configured_up=os.environ.get("CONFIGURED_UP"),
                 configured_down=os.environ.get("CONFIGURED_DOWN"),
                 udp_rate=os.environ.get("UDP_RATE") or None,
                 skew_s=skew))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
