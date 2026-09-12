"""Talk to the rig's router to read modem signal statistics.

Signal metrics (RSRP / RSRQ / SINR / band / cell ID) are the single most useful
coverage measurement available here and they cost zero cellular data — but the
rig's router runs ViaBot's own OpenWrt build, and its API has not been
characterised yet. So this module ships three clients:

``NullRouterClient``  the default; collects nothing, never fails.
``UbusRouterClient``  OpenWrt's standard ubus JSON-RPC over /ubus.
``LuciRouterClient``  the older LuCI RPC endpoint under /cgi-bin/luci/rpc.

Run ``python3 scripts/probe_router.py`` on the Pi to find out which one the
router answers to, then set ``router.client`` in config/config.yaml.

TLS note: the router serves a self-signed certificate on its own LAN address.
Certificate verification is therefore disabled *for requests to the router
only*. Nothing else in this project skips verification.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Any

# Keys we try to recognise in whatever shape the firmware returns. Values are
# the canonical field name used everywhere else in the app.
SIGNAL_ALIASES: dict[str, tuple[str, ...]] = {
    "rsrp": ("rsrp", "lte_rsrp", "nr_rsrp", "signal_rsrp"),
    "rsrq": ("rsrq", "lte_rsrq", "nr_rsrq", "signal_rsrq"),
    "sinr": ("sinr", "snr", "lte_sinr", "nr_sinr", "rssnr"),
    "rssi": ("rssi", "signal", "signal_strength"),
    "band": ("band", "lte_band", "nr_band", "current_band", "act_band"),
    "cell_id": ("cell_id", "cellid", "cid", "ci", "eci", "nci"),
    "tech": ("tech", "network_type", "mode", "rat", "access_tech", "connection_type"),
}

NUMERIC_FIELDS = {"rsrp", "rsrq", "sinr", "rssi"}
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _unverified_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = _NUMBER_RE.search(value)
        if match:
            return float(match.group())
    return None


def normalize_signal(payload: Any) -> dict[str, Any]:
    """Pull recognised signal fields out of an arbitrarily nested response.

    Firmwares disagree wildly about structure, so rather than hard-coding a
    path we walk the whole document and pick up keys we know by name. Shallower
    matches win, which keeps a top-level ``rsrp`` from being overwritten by one
    buried in a per-band detail list.
    """
    found: dict[str, Any] = {}
    depths: dict[str, int] = {}

    def walk(node: Any, depth: int) -> None:
        if isinstance(node, dict):
            for raw_key, value in node.items():
                key = str(raw_key).strip().lower()
                for canonical, aliases in SIGNAL_ALIASES.items():
                    if key not in aliases:
                        continue
                    parsed = _coerce_number(value) if canonical in NUMERIC_FIELDS else value
                    if parsed is None or parsed == "":
                        continue
                    if canonical not in found or depth < depths[canonical]:
                        found[canonical] = parsed if canonical in NUMERIC_FIELDS else str(parsed)
                        depths[canonical] = depth
                walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(payload, 0)
    return found


class RouterClient:
    """Interface implemented by every client."""

    name = "base"

    def fetch(self) -> dict[str, Any]:
        """Return normalised signal fields, or ``{}`` when unavailable."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class NullRouterClient(RouterClient):
    """Collects nothing. The safe default until the router API is known."""

    name = "null"

    def fetch(self) -> dict[str, Any]:
        return {}


class _HttpRouterClient(RouterClient):
    def __init__(self, address: str, username: str = "root", password: str = "",
                 scheme: str = "http", timeout: float = 5.0) -> None:
        self.address = address
        self.username = username
        self.password = password
        self.scheme = scheme
        self.timeout = timeout
        self._context = _unverified_context()

    def _post_json(self, path: str, payload: dict) -> Any:
        url = f"{self.scheme}://{self.address}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout,
                                    context=self._context) as response:
            return json.loads(response.read().decode("utf-8", "replace"))


class UbusRouterClient(_HttpRouterClient):
    """OpenWrt ubus JSON-RPC.

    Logs in once for a session token, then calls the configured object/method.
    If none is configured, falls back to ``network.interface.wan status``, which
    at least yields uptime and the WAN address on a stock build.
    """

    name = "ubus"
    ANONYMOUS_SESSION = "0" * 32

    def __init__(self, address: str, username: str = "root", password: str = "",
                 ubus_object: str | None = None, ubus_method: str | None = None,
                 **kwargs: Any) -> None:
        super().__init__(address, username, password, **kwargs)
        self.ubus_object = ubus_object or "network.interface.wan"
        self.ubus_method = ubus_method or "status"
        self._session: str | None = None
        self._request_id = 0

    def _call(self, session: str, obj: str, method: str, args: dict | None = None) -> Any:
        self._request_id += 1
        response = self._post_json("/ubus", {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "call",
            "params": [session, obj, method, args or {}],
        })
        if "error" in response:
            raise RuntimeError(f"ubus error: {response['error']}")
        result = response.get("result")
        # ubus returns [status_code, payload]; status 0 means success.
        if isinstance(result, list):
            if result and result[0] != 0:
                raise RuntimeError(f"ubus call failed with status {result[0]}")
            return result[1] if len(result) > 1 else {}
        return result

    def login(self) -> str:
        payload = self._call(self.ANONYMOUS_SESSION, "session", "login", {
            "username": self.username,
            "password": self.password,
        })
        token = (payload or {}).get("ubus_rpc_session")
        if not token:
            raise RuntimeError("ubus login returned no session token")
        self._session = token
        return token

    def fetch(self) -> dict[str, Any]:
        if self._session is None:
            self.login()
        try:
            payload = self._call(self._session, self.ubus_object, self.ubus_method)
        except (RuntimeError, urllib.error.HTTPError):
            # Sessions expire; retry once with a fresh login before giving up.
            self.login()
            payload = self._call(self._session, self.ubus_object, self.ubus_method)
        return normalize_signal(payload)

    def list_objects(self) -> Any:
        if self._session is None:
            self.login()
        self._request_id += 1
        response = self._post_json("/ubus", {
            "jsonrpc": "2.0", "id": self._request_id,
            "method": "list", "params": [self._session, "*"],
        })
        return response.get("result")


class LuciRouterClient(_HttpRouterClient):
    """Legacy LuCI RPC: authenticate at /cgi-bin/luci/rpc/auth, then run a shell
    command via the ``sys`` endpoint. Useful when the firmware exposes modem
    state only through an AT-command or vendor CLI helper."""

    name = "luci"

    def __init__(self, address: str, username: str = "root", password: str = "",
                 command: str | None = None, **kwargs: Any) -> None:
        super().__init__(address, username, password, **kwargs)
        # Overridable so a firmware-specific helper can be substituted.
        self.command = command or "gsmctl -A 'AT+CSQ'"
        self._token: str | None = None

    def login(self) -> str:
        response = self._post_json("/cgi-bin/luci/rpc/auth", {
            "id": 1, "method": "login", "params": [self.username, self.password],
        })
        token = response.get("result")
        if not token:
            raise RuntimeError("LuCI auth returned no token")
        self._token = token
        return token

    def fetch(self) -> dict[str, Any]:
        if self._token is None:
            self.login()
        response = self._post_json(
            f"/cgi-bin/luci/rpc/sys?auth={self._token}",
            {"id": 1, "method": "exec", "params": [self.command]},
        )
        raw = response.get("result") or ""
        return normalize_signal(_parse_at_output(raw))


def _parse_at_output(text: str) -> dict[str, Any]:
    """Turn ``+CSQ: 18,99`` / ``key: value`` AT output into a flat dict."""
    parsed: dict[str, Any] = {}
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line in ("OK", "ERROR"):
            continue
        if line.upper().startswith("+CSQ:"):
            numbers = _NUMBER_RE.findall(line)
            if numbers:
                # CSQ reports 0-31; convert to dBm the standard way.
                raw = int(float(numbers[0]))
                if raw != 99:
                    parsed["rssi"] = -113 + 2 * raw
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip().lstrip("+").lower()] = value.strip()
    return parsed


# ---------------------------------------------------------------------------
# Quectel +QENG parsing
# ---------------------------------------------------------------------------

#: Fields of ``+QENG: "servingcell",...`` for an LTE serving cell, in order,
#: after the leading ``"servingcell"`` and state tokens. Taken from Quectel's
#: LTE AT command manual and confirmed against the rig's own EP06-A: in a
#: reading of RSRP -104 / RSSI -78 on a 5 MHz carrier the 26 dB gap between
#: them matches 10*log10(300 subcarriers) = 24.8 dB, which only lines up if
#: the fields sit in this order.
QENG_LTE_FIELDS = (
    "is_tdd", "mcc", "mnc", "cell_id", "pcid", "earfcn", "band",
    "ul_bandwidth", "dl_bandwidth", "tac", "rsrp", "rsrq", "rssi",
    "sinr", "cqi",
)

#: Quectel reports bandwidth as an index, not a number of megahertz.
QENG_BANDWIDTH_MHZ = {0: 1.4, 1: 3.0, 2: 5.0, 3: 10.0, 4: 15.0, 5: 20.0}

#: States that mean "no serving cell to report", not "here is a bad one".
QENG_NO_CELL_STATES = {"SEARCH", "LIMSRV"}


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] == '"':
        return token[1:-1]
    return token


def parse_qeng(text: str) -> dict[str, Any]:
    """Parse ``AT+QENG="servingcell"`` output into canonical signal fields.

    Returns ``{}`` when the modem has no serving cell to report, when it
    answered ``ERROR``, or when the line is not one we understand — an empty
    reading is honest, an invented one is not.

    Only LTE is decoded field by field. The rig's modem is an EP06-A, which is
    LTE Cat 6 and cannot do anything else; if a different modem ever reports
    another technology we record what it is and leave the numbers alone rather
    than guessing at a layout we have never seen.
    """
    for line in str(text).splitlines():
        line = line.strip()
        if not line.upper().startswith("+QENG:"):
            continue
        tokens = [_unquote(t) for t in line.partition(":")[2].split(",")]
        if not tokens or tokens[0] != "servingcell":
            continue
        rest = tokens[1:]
        if not rest:
            continue
        state = rest[0].upper()
        if state in QENG_NO_CELL_STATES:
            return {}
        tech = rest[1].upper() if len(rest) > 1 else ""
        if tech != "LTE":
            return {"tech": tech} if tech else {}

        parsed: dict[str, Any] = {"tech": "LTE", "state": state}
        for name, value in zip(QENG_LTE_FIELDS, rest[2:]):
            if value in ("", "-"):
                continue
            if name in NUMERIC_FIELDS or name in ("pcid", "earfcn", "mcc", "mnc", "cqi"):
                number = _coerce_number(value)
                if number is not None:
                    parsed[name] = number
            else:
                parsed[name] = value

        bandwidth = _coerce_number(parsed.pop("dl_bandwidth", None))
        if bandwidth is not None:
            parsed["dl_bandwidth_mhz"] = QENG_BANDWIDTH_MHZ.get(int(bandwidth))
        parsed.pop("ul_bandwidth", None)
        return parsed
    return {}


def build_client(config: dict, router_address: str) -> RouterClient:
    """Construct the client named by ``router.client`` in the config."""
    kind = str(config.get("client", "null")).lower()
    if kind in ("null", "none", "off", ""):
        return NullRouterClient()
    common = {
        "username": config.get("username", "root"),
        "password": config.get("password", ""),
    }
    if kind == "ubus":
        return UbusRouterClient(
            router_address,
            ubus_object=config.get("ubus_object"),
            ubus_method=config.get("ubus_method"),
            **common,
        )
    if kind == "luci":
        return LuciRouterClient(router_address, scheme="https", **common)
    raise ValueError(f"unknown router client {kind!r} (expected null, ubus or luci)")
