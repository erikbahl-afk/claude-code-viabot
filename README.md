# ViaBot Garage Coverage Survey Rig

A battery-powered Raspberry Pi you carry through a parking garage to find where
the cellular link dies and whether those spots matter.

It measures the connection once a second while a camera records continuously.
Every measurement carries a pointer into the video, so when the link drops at
14:32:07 you can watch what the rig was pointed at 14:32:07 instead of guessing
from a spreadsheet.

The Pi broadcasts its own Wi-Fi network. Join it with your phone and the control
page opens by itself, like a hotel login page. That Wi-Fi is only the remote
control. The measurements go out the Pi's Ethernet port, through the router, and
out the router's 5G modem. That is the link under test.

## What you get

At the end of a walk:

* A percentage: how much of the walk had a usable connection.
* A list of dead zones, with start time, duration and severity.
* A video clip of each one, cut automatically. Ten seconds of approach, the dead
  zone itself however long it ran, five seconds of recovery. That clip is what
  tells you whether a dead spot is a ramp the robot has to drive or a corner
  nobody goes near.

Nothing to press while walking. The rig finds the dead zones itself.

The phone shows Start, Pause, End, a small connection readout, and a health
strip that goes red if the camera stops. Results are read afterwards on a
laptop.

Pause stops measuring and recording together, so time spent standing still
leaves no samples and no video. That is what keeps the percentage honest, since
it is a percentage of time and there is no indoor positioning.

## What it measures

| Signal | Cost | Default |
|---|---|---|
| ICMP ping: latency, packet loss, jitter | ~64 bytes/s | on |
| DNS resolution time | negligible | on |
| Modem stats: RSRP, RSRQ, SINR, band, cell ID | none | off, needs setup ([docs/ROUTER.md](docs/ROUTER.md)) |
| UDP load test: jitter and loss under a teleop-sized stream | high | off, needs a server ([docs/IPERF_SERVER.md](docs/IPERF_SERVER.md)) |
| iperf3 throughput | high | off, superseded by the UDP load test |

Ping and loss on their own locate dead zones well enough. The load tests stay
off in the example config because a fresh clone has no server to talk to.

A dead zone is 80% packet loss or 1500 ms latency, sustained for 5 seconds, with
zones less than 10 seconds apart merged into one. Those numbers are a
placeholder for "very bad" until a real walk shows what the robot cannot
tolerate. They live under `deadzone:` in the config, and a finished run can be
re-analysed with different ones without walking it again.

## Setup on a fresh Pi

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/erikbahl-afk/claude-code-viabot.git
cd claude-code-viabot
./scripts/preflight.sh     # read-only, answers most hardware questions
./scripts/setup.sh
```

`setup.sh` installs packages, builds a virtualenv, asks for a Wi-Fi password,
configures the access point and captive portal, then installs and starts the
systemd service. It is safe to run again at any time.

Then find out what the camera can do and put the suggested values into
`config/config.yaml`:

```bash
./scripts/probe_camera.sh
```

Full walkthrough in [docs/SETUP.md](docs/SETUP.md).

## Using it

1. Power on the rig and wait about a minute.
2. Join the Wi-Fi network on your phone (default SSID `ViaBot-Survey`).
3. The control page opens by itself. If it does not, go to
   <http://192.168.50.1/>.
4. Check the health strip is green, particularly Camera.
5. Type a location such as `Sunset Garage, Level 2` and press START.
6. Walk at a steady pace. Press PAUSE whenever you stop, take stairs, or move
   between areas.
7. Press END. The rig finds the dead zones, cuts the clips, and shows you the
   percentage.

You can lock the phone or switch apps while it runs. The Pi is doing the work.

If publishing is configured, reports upload themselves once the walk is over.
Otherwise pull results onto a laptop plugged into the router's spare LAN port:

```bash
./scripts/collect.sh --list
./scripts/collect.sh 20260911-143000-sunset-garage-l2
```

That brings down the clips, the video, and CSVs of every sample and dead zone.

## Updating the software

Claude pushes a branch and opens a pull request, you merge it, then press Apply
update on the dashboard. Step by step, assuming no git experience:
[docs/WORKFLOW.md](docs/WORKFLOW.md).

## Documentation

| | |
|---|---|
| [docs/CODE-TOUR.md](docs/CODE-TOUR.md) | How the code works, for someone reviewing it |
| [docs/SETUP.md](docs/SETUP.md) | First-time provisioning |
| [docs/UNVERIFIED.md](docs/UNVERIFIED.md) | What has never been checked on real hardware |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | Developing this with Claude if you are new to git |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the pieces fit together and why |
| [docs/HARDWARE.md](docs/HARDWARE.md) | Power chain, network chain, part numbers |
| [docs/ROUTER.md](docs/ROUTER.md) | Getting modem signal stats out of the router |
| [docs/IPERF_SERVER.md](docs/IPERF_SERVER.md) | Standing up the load-test server |
| [docs/PUBLISHING.md](docs/PUBLISHING.md) | Uploading reports and clips to the cloud |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | When something does not work |

## Project layout

```
viabot_survey/
  __main__.py        entry point, what systemd runs
  config.py          layered configuration
  runner.py          owns run state, samples at 1 Hz, analyses at run end
  storage.py         SQLite: runs, samples, dead zones, uploads, events
  app.py             Flask: captive portal, dashboard, JSON API
  deadzones.py       find dead zones in a finished run, cut their clips
  report.py          build a run's result and render the published page
  chart.py           throughput and radio plots as inline SVG, no library
  radio.py           RSRP and SINR into one score
  publish.py         resumable upload client
  capacity.py        one-off link ceiling test
  updater.py         git-based over-the-air updates
  sysinfo.py         host facts: clock sync, interfaces, disk, temperature
  router_client.py   modem statistics over SSH/AT, ubus or LuCI
  workers/           ping, dns, iperf, udpload, router, camera, publisher
  templates/ static/ the control page, no build step and no CDN
server/              the cloud receiver, deployed separately
scripts/             setup, AP config, update, probes, collect, capacity
tests/               pytest suite
config/config.example.yaml   every setting, documented inline
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest        # 316 tests, no camera or rig needed

# Run the app locally without the captive portal hijacking your browser:
.venv/bin/python -m viabot_survey --port 8080 --no-captive-portal
```

A few tests are marked `requires_ffmpeg` or `requires_iperf3` and skip when the
binary is missing.

## Safety notes

* This repository is public. `config/config.yaml` holds the Wi-Fi passphrase and
  the router password and is gitignored. Never commit it. `/api/config` masks
  secret-looking keys before returning anything.
* The control page has no authentication. Anyone in Wi-Fi range who knows the
  passphrase can start and stop runs.
* Video never leaves the Pi over Wi-Fi. It is written to local disk and either
  collected over the wired LAN or uploaded over cellular after the walk.
