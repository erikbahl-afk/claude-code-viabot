#!/usr/bin/env python3
"""Discover how the rig's router exposes modem signal statistics.

The router runs ViaBot's own OpenWrt build and its API has not been
characterised, so this script tries the known OpenWrt surfaces in turn and
reports which ones answer and what they return. Run it on the Pi:

    python3 scripts/probe_router.py --password 'the-router-password'

Then copy the suggested `router:` block it prints into config/config.yaml.

Read-only: it logs in and reads, and changes nothing on the router.
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
    else:
        print("""
router:
  client: "null"     # nothing usable found — leave signal collection off
""")
        detail("Nothing answered with recognisable signal fields.")
        detail("The rig still works: ping, loss and jitter already locate dead zones.")
        detail("Next step if you want signal metrics: SSH to the router and look for")
        detail("a vendor CLI (gsmctl, mmcli, ubus list) to read the modem directly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
