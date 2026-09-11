#!/usr/bin/env python3
"""Find a source of modem signal statistics for the rig.

Two places they could live, and we do not yet know which:

1. **The router's own API** (ubus or LuCI RPC). This is where they live on a
   normal OpenWrt cellular router.

2. **The modem's own embedded web interface.** The router reports its uplink as
   Protocol 5G on an *Ethernet adapter* called ``usb0`` — meaning the modem
   does its own NAT and presents to OpenWrt as a plain NIC. If that is right,
   OpenWrt has no modem to query at all: from its point of view the uplink is
   just a network card, and the radio metrics only exist inside the modem,
   typically behind a small web UI on its own subnet.

So this script probes both, in that order. Run it on the Pi:

    python3 scripts/probe_router.py --password 'the-router-password'

It prints a `router:` block to paste into config/config.yaml, or tells you
plainly that nothing usable was found.

Read-only throughout: it logs in, reads, and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from viabot_survey.router_client import (  # noqa: E402
    LuciRouterClient, UbusRouterClient, normalize_signal,
)

# ubus objects that carry modem state on the OpenWrt-derived firmwares that
# vendors actually ship. Tried in order; the first that yields signal fields wins.
CANDIDATE_CALLS: list[tuple[str, str]] = [
    ("gsm", "info"),
    ("gsm", "signal"),
    ("modem", "status"),
    ("modem", "info"),
    ("mobiled", "status"),
    ("network.interface.wan", "status"),
    ("network.interface.usb0", "status"),
    ("system", "info"),
    ("iwinfo", "info"),
]

# Addresses that USB cellular modems commonly self-host on. Tried only after
# the live traceroute hop, which is the reliable way to find it.
COMMON_MODEM_ADDRESSES = [
    "192.168.8.1",      # Huawei, many Quectel-based dongles
    "192.168.225.1",    # Quectel RG/RM series default
    "192.168.0.1",
    "192.168.100.1",    # several Sierra / Netgear units
    "192.168.32.1",
    "10.0.0.1",
]

# Paths worth asking a modem web UI for. The JSON/XML ones are vendor APIs that
# return signal data directly; "/" just tells us something is listening.
MODEM_API_PATHS = [
    "/api/device/signal",           # Huawei HiLink
    "/api/monitoring/status",       # Huawei HiLink
    "/cgi-bin/luci/admin/status",
    "/goform/goform_get_cmd_process?cmd=signalbar",   # ZTE
    "/status.json",
    "/api/status",
    "/",
]

# Shell commands worth trying through the LuCI exec endpoint, in order.
CANDIDATE_COMMANDS = [
    "gsmctl -A 'AT+CSQ'",
    "gsmctl -q",
    "gsmctl -A 'AT+QCSQ'",
    "gsmctl -A 'AT+QENG=\"servingcell\"'",
    "mmcli -m 0 --output-keyvalue",
    "ubus call gsm info",
    "cat /proc/net/dev",
]


def heading(text: str) -> None:
    print(f"\n\033[36m==>\033[0m {text}")


def detail(text: str) -> None:
    print(f"    {text}")


def reachable(address: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_probe(url: str, timeout: float = 5.0) -> str:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {exc}"


def next_hop_beyond(gateway: str, target: str = "8.8.8.8") -> str | None:
    """Find the router's own upstream gateway — most likely the modem.

    Uses a TTL-limited ping rather than traceroute so it needs no extra package:
    TTL 2 expires one hop past the router, and the ICMP "time exceeded" reply
    carries the address of whatever dropped it.
    """
    import re
    import subprocess

    try:
        result = subprocess.run(
            ["ping", "-c", "2", "-t", "2", "-W", "2", "-n", target],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if "Time to live exceeded" in line or "Time exceeded" in line:
            match = re.search(r"From ([\d.]+)", line)
            if match and match.group(1) != gateway:
                return match.group(1)
    return None


def try_modem_web_ui(router_address: str) -> dict | None:
    """Look for the modem's own web interface past the router."""
    heading("Modem's own web interface (past the router)")
    detail("The router reports its uplink as an Ethernet adapter doing its own")
    detail("NAT, so the radio metrics may only exist inside the modem itself.")

    candidates: list[str] = []
    hop = next_hop_beyond(router_address)
    if hop:
        detail(f"next hop beyond the router: {hop}  <-- most likely the modem")
        candidates.append(hop)
    else:
        detail("could not identify the next hop (ICMP may be filtered)")
    candidates += [a for a in COMMON_MODEM_ADDRESSES if a != router_address
                   and a not in candidates]

    for address in candidates:
        if not (reachable(address, 80, timeout=1.5) or reachable(address, 443, timeout=1.5)):
            continue
        detail(f"  {address}: something is listening")
        for path in MODEM_API_PATHS:
            url = f"http://{address}{path}"
            try:
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with urllib.request.urlopen(url, timeout=5, context=context) as response:
                    body = response.read(20000).decode("utf-8", "replace")
            except Exception:
                continue

            fields: dict = {}
            try:
                fields = normalize_signal(json.loads(body))
            except (json.JSONDecodeError, TypeError):
                # Vendor APIs often answer in XML; a crude key scrape is enough
                # to tell whether the numbers are in there at all.
                import re as _re
                pairs = dict(_re.findall(r"<(\w+)>([^<]+)</\1>", body))
                if pairs:
                    fields = normalize_signal(pairs)
            marker = "  <-- usable" if fields else ""
            detail(f"    {path}: HTTP 200, {len(body)} bytes, "
                   f"signal fields {sorted(fields) or 'none'}{marker}")
            if fields:
                return {"address": address, "path": path, "fields": fields,
                        "body": body[:2000]}
        detail(f"    nothing at {address} returned recognisable signal fields")
    if not candidates:
        detail("no candidate modem addresses found")
    return None


def try_ubus(address: str, username: str, password: str) -> dict | None:
    heading(f"ubus JSON-RPC at http://{address}/ubus")
    client = UbusRouterClient(address, username=username, password=password)
    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        detail(f"login failed: {exc}")
        return None
    detail("login OK")

    try:
        objects = client.list_objects()
        if isinstance(objects, dict):
            names = sorted(objects)
            detail(f"{len(names)} ubus objects exposed")
            interesting = [n for n in names
                           if any(k in n.lower()
                                  for k in ("gsm", "modem", "mobile", "sim", "signal", "3g", "lte"))]
            if interesting:
                detail(f"  modem-looking objects: {', '.join(interesting)}")
                for name in interesting:
                    for method in sorted((objects.get(name) or {})):
                        CANDIDATE_CALLS.insert(0, (name, method))
            else:
                detail("  none obviously modem-related; will try the standard list")
    except Exception as exc:  # noqa: BLE001
        detail(f"could not list objects: {exc}")

    best: dict | None = None
    seen: set[tuple[str, str]] = set()
    for obj, method in CANDIDATE_CALLS:
        if (obj, method) in seen:
            continue
        seen.add((obj, method))
        try:
            payload = client._call(client._session, obj, method)  # noqa: SLF001
        except Exception:
            continue
        fields = normalize_signal(payload)
        marker = "  <-- usable" if fields else ""
        detail(f"  {obj} {method}: {len(json.dumps(payload))} bytes, "
               f"signal fields {sorted(fields) or 'none'}{marker}")
        if fields and best is None:
            best = {"object": obj, "method": method, "fields": fields, "raw": payload}
    return best


def try_luci(address: str, username: str, password: str) -> dict | None:
    heading(f"LuCI RPC at https://{address}/cgi-bin/luci/rpc")
    client = LuciRouterClient(address, username=username, password=password)
    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        detail(f"login failed: {exc}")
        return None
    detail("login OK")

    for command in CANDIDATE_COMMANDS:
        client.command = command
        try:
            fields = client.fetch()
        except Exception as exc:  # noqa: BLE001
            detail(f"  {command!r}: failed ({exc})")
            continue
        marker = "  <-- usable" if fields else ""
        detail(f"  {command!r}: signal fields {sorted(fields) or 'none'}{marker}")
        if fields:
            return {"command": command, "fields": fields}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default="192.168.1.1", help="router LAN address")
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="", help="router admin password")
    parser.add_argument("--json", action="store_true", help="dump the raw payload that worked")
    args = parser.parse_args()

    heading(f"Reachability of {args.address}")
    for port, label in ((80, "http"), (443, "https"), (22, "ssh")):
        detail(f"  port {port} ({label}): {'open' if reachable(args.address, port) else 'closed'}")
    detail(f"  GET http://{args.address}/ubus -> {http_probe(f'http://{args.address}/ubus')}")
    detail(f"  GET https://{args.address}/cgi-bin/luci/ -> "
           f"{http_probe(f'https://{args.address}/cgi-bin/luci/')}")

    if not args.password:
        print("\n\033[33m!\033[0m No --password given. Login will almost certainly fail;")
        print("  re-run with the router admin password to get useful results.")

    ubus = try_ubus(args.address, args.username, args.password)
    luci = None if ubus else try_luci(args.address, args.username, args.password)
    modem = None if (ubus or luci) else try_modem_web_ui(args.address)

    heading("Suggested config/config.yaml")
    if ubus:
        print(f"""
router:
  client: "ubus"
  interval_s: 2
  username: {args.username}
  password: "<router password>"
  ubus_object: {ubus['object']}
  ubus_method: {ubus['method']}
""")
        detail(f"fields this returns: {ubus['fields']}")
        if args.json:
            print(json.dumps(ubus["raw"], indent=2)[:4000])
    elif luci:
        print(f"""
router:
  client: "luci"
  interval_s: 5
  username: {args.username}
  password: "<router password>"
""")
        detail(f"working command: {luci['command']}")
        detail("Set LuciRouterClient.command in viabot_survey/router_client.py to match.")
        detail(f"fields this returns: {luci['fields']}")
    elif modem:
        print(f"""
# The modem, not the router, is serving these. Paste this output into a Claude
# session and a client for it can be added — the URL shape is vendor-specific.
#   address: {modem['address']}
#   path:    {modem['path']}
#   fields:  {sorted(modem['fields'])}
router:
  client: "null"     # until a modem client exists
""")
        detail("Raw response (first 2000 bytes):")
        print(modem["body"])
    else:
        print("""
router:
  client: "null"     # nothing usable found — leave signal collection off
""")
        detail("Nothing answered with recognisable signal fields.")
        detail("The rig still works: ping, loss and jitter already locate dead zones,")
        detail("and that is what the camera correlation is built around.")
        detail("")
        detail("If you want to push further, in rough order of likely payoff:")
        detail("  1. Browse the router's LuCI menus for a Modem/Mobile/Cellular page.")
        detail("     Only the front Status page has ever been looked at.")
        detail("  2. SSH to the router and try: ubus list; gsmctl -A 'AT+CSQ'; mmcli -L")
        detail("  3. Find the modem's own web UI — check the router's WAN address and")
        detail("     gateway under Network > Interfaces, then browse to that gateway.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
