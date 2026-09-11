# ViaBot Garage Coverage Survey Rig

A battery-powered Raspberry Pi rig you carry through a parking garage to find
out **where the cellular link dies, and whether those spots matter**.

It measures the connection continuously while a camera records continuously.
Every measurement carries a pointer into the video, so when the link drops at
14:32:07 you can look at what the rig was pointed at at 14:32:07 — an entrance,
a ramp between levels, a charging dock — rather than guessing from a spreadsheet
of timestamps.

The Pi broadcasts its own Wi-Fi network. Join it with your phone and the control
dashboard opens by itself, the way a hotel Wi-Fi login page does. The
measurements do **not** go over that Wi-Fi — they go out the Pi's Ethernet port,
through the router, through the router's 5G modem. That is the link under test.

---

## What you get

- **Live status while you walk.** A full-width colour banner — GOOD / DEGRADED /
  BAD / DEAD ZONE — with RTT and packet loss in numbers big enough to read at
  arm's length in a dark garage. The phone buzzes when you enter a dead zone.
- **One-thumb marking.** Tap a category chip (`Ramp`, `Entrance`, `Elevator`, …)
  and hit the big **MARK** button to tag where you are. This replaces the
  physical GPIO button from the original plan — no extra hardware to source.
- **Continuous video** with the wall clock burned into the picture, split into
  5-minute files named by start time.
- **A report per run** that groups contiguous bad stretches into "problem areas"
  and tells you exactly which video file and offset to open for each one.
- **CSV export** of every sample and every mark.
- **Updates without a terminal.** Merge a pull request on GitHub, then press
  "Apply update" on the dashboard.

## What it measures

| Signal | Cost | Default |
|---|---|---|
| ICMP ping — RTT, packet loss, jitter | ~64 bytes/s | **on** |
| DNS resolution time | negligible | **on** |
| Modem stats — RSRP, RSRQ, SINR, band, cell ID | none | off, needs discovery — see [docs/ROUTER.md](docs/ROUTER.md) |
| iperf3 throughput | **high** | off, needs a server — see [docs/IPERF_SERVER.md](docs/IPERF_SERVER.md) |

Ping and loss alone locate dead zones perfectly well. Throughput testing is off
until you stand up your own iperf3 server, because it is the only part of the
rig that spends real cellular data.

---

## Setup on a fresh Pi

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/erikbahl-afk/claude-code-viabot.git
cd claude-code-viabot
./scripts/preflight.sh     # read-only; paste the output into a Claude session
./scripts/setup.sh
```

Run `preflight.sh` first. Several things this rig depends on have never actually
been verified on the hardware — most importantly whether the Pi's Wi-Fi can run
as an access point at all, which the entire control plane assumes. It changes
nothing and takes a few seconds.

The script installs packages, builds a virtualenv, asks you for a Wi-Fi
password, configures the access point and captive portal, installs the systemd
service and starts it. It is safe to run again at any time.

Then, still on the Pi, find out what the camera can do and put the suggested
values into `config/config.yaml`:

```bash
./scripts/probe_camera.sh
```

Full walkthrough: **[docs/SETUP.md](docs/SETUP.md)**.

## Using it

1. Power on the rig. Wait about a minute.
2. On your phone, join the Wi-Fi network (default SSID `ViaBot-Survey`).
3. The dashboard opens on its own. If it does not, browse to
   <http://192.168.50.1/>.
4. Type a label (`Sunset Garage L2`), press **Start run**.
5. Walk. Watch the banner. Tap **MARK** wherever the reading matters —
   especially at ramps, entrances and anywhere the robot has to drive.
6. Press **Stop run**.
7. Open **Runs → Report** to see the problem areas and which video to review.

Pull the data onto your laptop afterwards (run this **from the laptop**, plugged
into the router's spare LAN port):

```bash
./scripts/collect.sh --list
./scripts/collect.sh 20260911-143000-sunset-garage-l2
```

## Updating the software

You talk to Claude in the browser; Claude pushes a branch and opens a pull
request; you merge it; the rig pulls it down. Step-by-step, assuming no git
experience: **[docs/WORKFLOW.md](docs/WORKFLOW.md)**.

---

## Documentation

| | |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | First-time provisioning, in detail |
| [docs/UNVERIFIED.md](docs/UNVERIFIED.md) | What has never been checked on the real hardware |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | How to develop this with Claude if you're new to git |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit together and why |
| [docs/HARDWARE.md](docs/HARDWARE.md) | The physical rig: power chain, network chain, part numbers |
| [docs/ROUTER.md](docs/ROUTER.md) | Getting modem signal statistics out of the router |
| [docs/IPERF_SERVER.md](docs/IPERF_SERVER.md) | Standing up a throughput server, and the data-use warning |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | When something doesn't work |

## Project layout

```
viabot_survey/          the application
  __main__.py           entry point (what systemd runs)
  config.py             layered configuration
  runner.py             orchestrates workers, owns run state, classifies samples
  storage.py            SQLite: runs, samples, marks, throughput, events
  app.py                Flask: captive portal, dashboard, JSON API
  updater.py            git-based over-the-air updates
  sysinfo.py            host facts (clock sync, interfaces, disk, temperature)
  router_client.py      modem statistics over ubus / LuCI
  workers/              ping, dns, iperf3, router, camera
  templates/, static/   the dashboard (no build step, no CDN)
scripts/                setup, AP config, update, probes, uninstall, collect
tests/                  pytest suite
config/config.example.yaml   every setting, documented inline
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest              # the suite runs anywhere, no hardware needed

# Run the app on a laptop, without the captive portal hijacking your browser:
.venv/bin/python -m viabot_survey --port 8080 --no-captive-portal
```

## Safety notes

- **This repository is public.** `config/config.yaml` holds the Wi-Fi
  passphrase and the router password and is gitignored. Never commit it. The
  `/api/config` endpoint masks secret-looking keys before returning anything.
- The dashboard has **no authentication**. Anyone within Wi-Fi range who knows
  the passphrase can start and stop runs. Treat the passphrase as the only lock
  on the rig.
- Video never leaves the Pi over Wi-Fi — it is written to local disk and
  collected over the wired LAN.
