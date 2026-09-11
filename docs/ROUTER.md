# Modem signal statistics

Ping and loss tell you *that* the link failed. RSRP, RSRQ, SINR, band and cell
ID tell you *why* — weak signal, interference, or a handover to a distant cell —
and they cost no cellular data at all.

They have to come from somewhere inside the rig, and it is not yet clear where.

The router's LuCI front page reports its uplink as **Protocol 5G on an Ethernet
adapter called `usb0`** — meaning the modem does its own NAT and presents to
OpenWrt as a plain network card. If that reading is right, the router has no
modem to query at all: from its point of view the uplink is just a NIC, and the
radio metrics live inside the modem, typically behind a small web interface on
its own subnet.

So there are two candidate sources, and the discovery script probes both:

1. the router's own API (ubus or LuCI RPC) — where they live on a conventional
   OpenWrt cellular router;
2. the modem's own embedded web UI, past `usb0`.

Only the router's front Status page has ever been looked at — no Modem or
Cellular submenu was ever opened, and nobody has SSH'd into the router. Until
that changes the rig ships with signal collection **off**.

## What we know so far

A preflight run on the rig traced the next hop past the router to
**192.168.225.1**, reachable from the Pi. That address is the factory default
for Quectel RG/RM-series modules, which supports the reading that the modem
does its own NAT and presents to OpenWrt as a plain Ethernet adapter — so the
router very likely has no modem to query, and the signal metrics live at that
address instead.

The quickest check is to open <http://192.168.225.1/> in a browser on a machine
that can reach the rig's LAN and see what it serves. The probe script below
tries it automatically.

## Discover the API

On the Pi:

```bash
python3 scripts/probe_router.py --password '<router admin password>'
```

It is read-only — it logs in and reads, and changes nothing. It will:

1. check which ports the router answers on;
2. try to log in over **ubus** (`http://192.168.1.1/ubus`), OpenWrt's standard
   JSON-RPC, and enumerate the objects it exposes;
3. call every object whose name looks modem-related (`gsm`, `modem`, `mobiled`,
   `sim`, `signal`, …) plus a list of standard ones, reporting which returned
   recognisable signal fields;
4. if ubus is unavailable, fall back to the older **LuCI RPC** endpoint and try
   a series of vendor CLI commands (`gsmctl -A 'AT+CSQ'`, `mmcli`, …);
5. if neither answers, find the **modem** itself: a TTL-limited ping locates
   the next hop past the router, and the script then asks that address for the
   vendor API paths these modems commonly serve (`/api/device/signal`,
   `/goform/goform_get_cmd_process?cmd=signalbar`, …), falling back to a list
   of default modem addresses;
6. print a `router:` block ready to paste into `config/config.yaml` — or, if it
   found the modem instead, the raw response to paste into a Claude session so
   a client can be written for it.

Add `--json` to dump the raw payload of whatever worked, which is useful to
paste into a Claude session if the field names need new aliases.

## Configure it

```yaml
router:
  client: "ubus"          # or "luci", or "null" to collect nothing
  interval_s: 2
  username: root
  password: "<router admin password>"
  ubus_object: gsm        # whatever the probe found
  ubus_method: info
```

Restart: `sudo systemctl restart viabot-survey`.

The dashboard's third metric tile switches to **RSRP** (falling back to SINR,
then jitter) once readings arrive, and the values are stored on every sample and
included in the CSV export.

> `config/config.yaml` is gitignored, and `/api/config` masks anything that
> looks like a secret. Still — this repository is public. Do not paste the
> router password into an issue, a commit or a Claude session transcript.

## If nothing works

That is a fine outcome. The rig's primary signal is ping loss and latency, and
that already locates dead zones precisely. Leave `client: "null"`.

If you want to push further, SSH to the router and look around by hand:

```bash
ssh root@192.168.1.1
ubus list                        # what services exist
ubus call <object> <method>      # try them
gsmctl -A 'AT+CSQ'               # Teltonika-style vendor CLI
mmcli -L && mmcli -m 0           # ModemManager, if present
ls /sys/class/net/               # confirm the modem interface name
```

Paste what you find into a Claude session and it can add a client for it.

## Reading the numbers

| Metric | Good | Usable | Poor |
|---|---|---|---|
| RSRP (dBm) | > −80 | −80 to −100 | < −110 |
| RSRQ (dB) | > −10 | −10 to −15 | < −20 |
| SINR (dB) | > 20 | 5 to 20 | < 0 |

In a concrete garage expect RSRP to fall steeply as you descend. The
interesting moments are where SINR collapses while RSRP stays reasonable —
that is interference or a cell-edge handover rather than simple attenuation,
and it often explains a dead zone that "looks" like it should have coverage.

A changing **cell ID** mid-walk means a handover, which is worth correlating
with the video: handovers at a ramp between levels are a recurring cause of
dropped robot connections.
