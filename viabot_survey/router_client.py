"""Talk to the rig's router to read modem signal statistics.

Signal metrics (RSRP / RSRQ / SINR / band / cell ID) are the single most useful
coverage measurement available here, and they cost zero cellular data. Getting
at them is the hard part.

The rig's router was probed and has no API to ask: ``/ubus`` returns 404, there
is no LuCI RPC, HTTPS is closed, and ``ubus list`` carries no modem object —
only network interfaces. There is no vendor CLI either. The radio metrics exist
nowhere on the router at all; they live inside the modem, behind its AT port,
which is reachable only from a shell on the router. Hence ``at_ssh``, which is
what the rig actually uses.

``NullRouterClient``       collects nothing, never fails. Still the default.
``AtOverSshRouterClient``  SSH to the router, talk AT to the modem. Works here.
``UbusRouterClient``       OpenWrt ubus JSON-RPC. Kept for a router that has it.
``LuciRouterClient``       the older LuCI RPC endpoint. Same.

Run ``python3 scripts/probe_router.py`` on the Pi against an unfamiliar router
to see which of these it answers to, then set ``router.client``.

TLS note: the router serves a self-signed certificate on its own LAN address.
Certificate verification is therefore disabled *for requests to the router
only*. Nothing else in this project skips verification.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

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
    """Collects nothing. The safe default, and the fallback for a bad config.

    ``reason`` says why, when it is standing in for a client that could not be
    built. It reaches the worker status and so the API, which is the only place
    a misconfiguration would otherwise be visible at all.
    """

    name = "null"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

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

        # Named modem_state, not state: this is the modem's view of its own
        # connection ("NOCONN", "CONNECT"), and a worker already has a state
        # meaning something else entirely.
        parsed: dict[str, Any] = {"tech": "LTE", "modem_state": state}
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


# ---------------------------------------------------------------------------
# AT commands over SSH
# ---------------------------------------------------------------------------

#: The modem's AT command port on the rig's router. Quectel modems expose four
#: serial ports; ttyUSB2 is the AT interpreter on every one we have seen.
DEFAULT_AT_DEVICE = "/dev/ttyUSB2"

#: The one command we need. ``servingcell`` reports the cell the modem is
#: actually attached to, with its signal levels, which is the whole point.
DEFAULT_AT_COMMAND = 'AT+QENG="servingcell"'

#: SSH options every connection needs.
#:
#: The two ``+ssh-rsa`` lines are not optional: the router's Dropbear offers
#: only an RSA host key, which modern OpenSSH refuses by default, and without
#: them the connection fails before asking for a password.
#:
#: Host key checking is off *for this connection only*, and deliberately. The
#: rig meets a different router at the same address every time one is swapped
#: out of the fleet, and the last swap stopped SSH dead with a host-key warning
#: that took a human with a keyboard to clear. A rig that quietly stops
#: recording signal halfway through a survey because a router was replaced is a
#: worse outcome than the risk being guarded against here, which is someone
#: physically splicing the Ethernet cable between the Pi and its own router.
SSH_OPTIONS = (
    "-o", "HostKeyAlgorithms=+ssh-rsa",
    "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=10",
    # Notice a dead link in ~15 s rather than blocking until TCP gives up.
    "-o", "ServerAliveInterval=5",
    "-o", "ServerAliveCountMax=3",
)


def at_stream_script(device: str = DEFAULT_AT_DEVICE,
                     command: str = DEFAULT_AT_COMMAND,
                     interval_s: float = 2.0) -> str:
    """Shell for the router that streams one modem reading per interval.

    The naive approach — one SSH connection per reading — spends most of a
    survey doing TCP and crypto handshakes. Instead a single connection holds
    one reader on the port for the whole walk and pokes the modem on a timer,
    so the cost per reading is a printf.

    Reading and writing are separate because the port is a character device,
    not a request/response socket: ``cat`` must already be listening when the
    command goes in or the reply is lost to nobody.

    The stale-reader sweep at the top matters more than it looks. A ``cat`` left
    over from a dropped connection steals characters from the new one, and the
    symptom is not silence but readings that arrive torn in half — which looks
    like a hardware fault and is not one.
    """
    interval = max(1.0, float(interval_s))
    return "\n".join([
        f"for p in $(ps | grep '[c]at {device}' | awk '{{print $1}}'); "
        "do kill \"$p\" 2>/dev/null; done",
        "trap 'kill $R 2>/dev/null' EXIT INT TERM HUP",
        f"cat {device} & R=$!",
        "while kill -0 $R 2>/dev/null; do",
        f"printf '{command}\\r\\n' > {device} || break",
        f"sleep {interval:g}",
        "done",
    ])


class AtOverSshRouterClient(RouterClient):
    """Read modem signal over SSH to the router, by talking AT to the modem.

    This router has no API to ask: no ubus over HTTP, no LuCI RPC, no vendor
    CLI, no modem object on the bus at all. The radio metrics exist only inside
    the modem, reachable only through its AT port, which is reachable only from
    a shell on the router. So that is what this does.

    Authentication is by SSH key when one is set up, and by password otherwise.
    Neither is better in every case, so both work: a key leaves no secret on the
    Pi but has to be installed on each router, while a password needs nothing
    done to the router — which matters for a rig that meets a different unit out
    of the fleet each time — at the cost of living in ``config/config.yaml``.
    That file already holds the Wi-Fi passphrase, is gitignored, and is redacted
    before anything reaches the database or the API.
    """

    name = "at_ssh"

    def __init__(self, address: str, username: str = "root", password: str = "",
                 device: str = DEFAULT_AT_DEVICE, command: str = DEFAULT_AT_COMMAND,
                 interval_s: float = 2.0, **kwargs: Any) -> None:
        self.address = address
        self.username = username or "root"
        self.password = password or ""
        self.device = device or DEFAULT_AT_DEVICE
        self.command = command or DEFAULT_AT_COMMAND
        self.interval_s = max(1.0, float(interval_s))
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, Any] = {}
        self._latest_ts: float = 0.0
        self._stderr: str = ""
        self._last_start: float = 0.0

    # -- command -------------------------------------------------------------

    def build_command(self) -> list[str]:
        """The full argv, ready to run. Separate so it can be tested."""
        script = at_stream_script(self.device, self.command, self.interval_s)
        ssh = ["ssh", *SSH_OPTIONS]
        if self.password:
            # -e reads the password from the environment, not the command line.
            # sshpass -p would put it in argv, where every user on the Pi could
            # read it out of ps for as long as the survey runs.
            ssh = ["sshpass", "-e", *ssh,
                   "-o", "PubkeyAuthentication=no",
                   "-o", "PreferredAuthentications=password"]
        else:
            ssh += ["-o", "BatchMode=yes"]
        return [*ssh, f"{self.username}@{self.address}", script]

    def build_env(self) -> dict[str, str] | None:
        """Environment for the SSH process: where the password travels."""
        if not self.password:
            return None
        return {**os.environ, "SSHPASS": self.password}

    def missing_tool(self) -> str | None:
        """Name whichever required binary is not installed, if any."""
        for tool in (["sshpass"] if self.password else []) + ["ssh"]:
            if shutil.which(tool) is None:
                return tool
        return None

    # -- stream --------------------------------------------------------------

    #: Never reconnect faster than this. A wrong password makes ssh exit at
    #: once, and without a floor the rig would retry it every poll for the
    #: length of a survey — a good way to be throttled or locked out by a
    #: router that counts failed logins.
    MIN_RECONNECT_S = 15.0

    def _start(self) -> None:
        since = time.monotonic() - self._last_start
        if self._last_start and since < self.MIN_RECONNECT_S:
            raise RuntimeError(
                self._stderr or f"waiting {self.MIN_RECONNECT_S - since:.0f}s to reconnect")
        missing = self.missing_tool()
        if missing:
            raise RuntimeError(f"{missing} is not installed")
        self._last_start = time.monotonic()
        self._stderr = ""
        self._proc = subprocess.Popen(
            self.build_command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
            env=self.build_env(),
        )
        self._reader = threading.Thread(target=self._read, args=(self._proc,),
                                        name="at-ssh-reader", daemon=True)
        self._reader.start()

    def _read(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                fields = parse_qeng(line)
                if not fields:
                    continue
                with self._lock:
                    self._latest = fields
                    self._latest_ts = time.time()
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._stderr = (proc.stderr.read() or "").strip()[:300]  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- RouterClient --------------------------------------------------------

    def fetch(self) -> dict[str, Any]:
        """Return the newest reading, or raise so the worker shows degraded."""
        if not self._alive():
            self.close()
            self._start()

        with self._lock:
            latest, age = dict(self._latest), time.time() - self._latest_ts

        # Three intervals of silence means the stream is up but the modem is
        # not answering — a real condition, and not one to report stale numbers
        # through. Ten seconds of grace covers the first connection.
        if not latest or age > max(10.0, self.interval_s * 3):
            detail = self._stderr or f"no reading for {age:.0f}s"
            raise RuntimeError(f"no modem reading: {detail}")
        return latest

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.join(timeout=2)


def build_client(config: dict, router_address: str,
                 strict: bool = False) -> RouterClient:
    """Construct the client named by ``router.client`` in the config.

    A name we do not recognise falls back to collecting nothing rather than
    raising. Signal metrics are an optional extra — the rig finds dead zones
    from ping loss and latency with or without them — and a typo in this one
    key used to crash the service on startup, which took the whole rig down and
    with it the dashboard that is how an update gets applied. Being unable to
    fix a typo without a keyboard and an SSH session is a far worse failure than
    walking a garage with no RSRP.

    Pass ``strict`` where a human is waiting on the answer, as the probe script
    and the tests do, so a typo is reported rather than quietly tolerated.
    """
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
    if kind in ("at_ssh", "at-ssh", "at"):
        return AtOverSshRouterClient(
            router_address,
            device=config.get("at_device") or DEFAULT_AT_DEVICE,
            command=config.get("at_command") or DEFAULT_AT_COMMAND,
            interval_s=float(config.get("interval_s", 2) or 2),
            **common,
        )
    message = f"unknown router client {kind!r} (expected null, at_ssh, ubus or luci)"
    if strict:
        raise ValueError(message)
    log.warning("%s - collecting no signal metrics", message)
    return NullRouterClient(reason=message)
