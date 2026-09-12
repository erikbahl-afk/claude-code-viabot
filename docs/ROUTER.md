# Modem signal statistics

Ping and loss tell you *that* the link failed. RSRP, RSRQ, SINR, band and cell
ID tell you *why* — weak signal, interference, or a handover to a distant cell —
and they cost no cellular data at all.

Getting at them turned out to be the hard part, and this page records how it
was settled so nobody repeats the search.

## Where they are not

The rig's standard field router (OpenWrt SNAPSHOT `r9104-fe7d965`) was probed
from the Pi and then by hand over SSH on **2026-09-12**. It has no modem API of
any kind:

| Checked | Result |
|---|---|
| `http://192.168.1.1/ubus` | **404** — uhttpd's ubus proxy is not built in |
| `https://192.168.1.1/cgi-bin/luci/rpc` | port 443 closed; no LuCI RPC |
| `ubus list` on the router | `block dhcp dnsmasq log mwan3 network* service session system uci` — **no modem object** |
| `gsmctl` | not installed (it is Teltonika-specific) |
| `mmcli` / ModemManager | not installed |
| `curl` | not installed (`wget` is) |

So there is nothing on the router to ask. The radio metrics exist only inside
the modem.

## Where they are

The modem is a **Quectel EP06-A** — LTE Cat 6, not 5G, whatever LuCI's
"Protocol: 5G" interface label suggests. It exposes four serial ports on the
router, and `/dev/ttyUSB2` is its AT command interpreter:

```
root@HL:~# cat /dev/ttyUSB2 & P=$!; sleep 1
root@HL:~# printf 'AT+QENG="servingcell"\r\n' > /dev/ttyUSB2; sleep 2; kill $P
+QENG: "servingcell","NOCONN","LTE","FDD",310,260,1452808,315,5035,12,2,2,3AFD,-104,-11,-78,12,20
```

That single line carries everything: T-Mobile (310/260), band 12, a 5 MHz
carrier, cell 1452808, RSRP −104 dBm, RSRQ −11 dB, RSSI −78 dBm, SINR 12 dB,
CQI 20.

`AT+QRSRP` returns `ERROR` on this firmware. `AT+QENG="servingcell"` is the one
that works, and it is the one the rig uses.

### One caution about the field order

Nothing in the reply labels its fields; they are positional, and Quectel's
manuals differ between modules. The order the parser assumes is checked against
this reading: it reports RSRP and RSSI 26 dB apart on a 5 MHz carrier, and a
5 MHz carrier spreads across 300 subcarriers, which is 10·log₁₀(300) = 24.8 dB.
Those agree, which they would not if the fields sat anywhere else. The test
`test_a_real_serving_cell_line_parses` pins that exact line for this reason.

## Turn it on

Set two keys in `config/config.yaml` on the Pi:

```yaml
router:
  client: "at_ssh"
  password: "<the router's root password>"
```

Then `sudo systemctl restart viabot-survey`. Readings appear on every sample
within a few seconds, and in the CSV export.

The Pi holds one SSH connection open to the router for the whole walk, with a
single reader on the AT port, and pokes the modem every `interval_s` seconds.
A connection per reading would spend most of a survey on handshakes.

### Without storing the password

If you would rather no secret lived on the Pi, use an SSH key instead. Leave
`password: ""` and, once per router:

```bash
# On the Pi, as the user the service runs as (viabot):
ssh-keygen -t rsa -b 2048 -N '' -f ~/.ssh/id_rsa      # skip if you have one
cat ~/.ssh/id_rsa.pub
```

Paste that line into `/etc/dropbear/authorized_keys` on the router (the file
does not exist yet; create it), `chmod 600` it, and test with
`ssh root@192.168.1.1 'echo ok'` — it should not ask for a password.

Neither route is better in every case. The key leaves nothing secret on the SD
card but has to be installed on **every router the rig ever meets**, and is lost
if one is reflashed. The password needs nothing done to the router at all, which
is what matters when a unit is swapped out of the fleet mid-survey — at the cost
of sitting in a file on the Pi.

> `config/config.yaml` is gitignored, and every value under a key named
> `password` is masked before it reaches the database, the API or the
> dashboard. Still — this repository is public. Do not paste the router password
> into an issue, a commit, or a Claude session.

## An unfamiliar router

If ViaBot's fleet ever carries something else, probe it before assuming:

```bash
python3 scripts/probe_router.py --password '<router admin password>'
```

It is read-only. It checks which ports answer, tries ubus and LuCI RPC, hunts
for the modem's own web interface past the router, and prints a `router:` block
to paste into the config — or says plainly that nothing usable was found, which
is what happened here.

`client: "null"` is always a valid answer. The rig's primary signal is ping loss
and latency, and that already locates dead zones precisely; signal metrics
explain them, they do not find them.

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
An antenna fixes the first kind and not the second.

A changing **cell ID** mid-walk means a handover, which is worth correlating
with the video: handovers at a ramp between levels are a recurring cause of
dropped robot connections.
