# Throughput testing with iperf3

Shipped **disabled**. Ping and packet loss already locate dead zones, and this
is the only part of the rig that spends real cellular data.

## Read this before you enable it

A 5-second iperf3 test capped at 25 Mbit/s moves about **15 MB**. Run once a
minute for a 30-minute walk, that is roughly **0.5 GB**. Uncapped on a good 5G
signal it is easily **10–20 GB** for the same walk.

Check what the SIM's plan actually allows. A fleet SIM that gets throttled to
128 kbit/s halfway through your survey will produce a map of your data cap
rather than a map of the garage.

Three guards exist, and you should keep all three:

| Setting | Default | What it does |
|---|---|---|
| `bitrate` | `25M` | Caps each test. A capped test still finds dead zones — you learn where the link cannot even sustain the cap. |
| `interval_s` | `60` | Seconds between tests. |
| `run_data_budget_mb` | `2000` | Stops launching tests once one run has moved this much. |

There is also a **Run throughput test** button on the dashboard for a single
on-demand test, which is often all you need.

## Step 1 — Stand up a server

Use a cheap VPS (DigitalOcean, Hetzner, Lightsail — about $5/month). Pick a
region geographically near the garages you survey; you are measuring the local
radio link, and a server three continents away adds latency that has nothing to
do with the garage.

**Do not use the public iperf3 servers for real surveys.** They are shared and
rate-limited, so a bad result tells you the server was busy, not that the
coverage was poor. They are fine for a one-off sanity check.

On the server:

```bash
sudo apt update && sudo apt install -y iperf3

sudo tee /etc/systemd/system/iperf3.service >/dev/null <<'UNIT'
[Unit]
Description=iperf3 server
After=network.target

[Service]
ExecStart=/usr/bin/iperf3 --server --port 5201
Restart=always
User=nobody
DynamicUser=yes

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl enable --now iperf3
```

### Lock it down

An open iperf3 server is free bandwidth for anyone who finds it, and you are
paying for the egress. Restrict it to the addresses your rig actually appears
from:

```bash
sudo ufw allow from <your-cellular-egress-range> to any port 5201 proto tcp
sudo ufw enable
```

To find that address, from the Pi: `curl -s https://api.ipify.org`. Carrier NAT
means it will move around — expect a range, and re-check it occasionally. If
that proves too fiddly, use iperf3's shared-secret authentication (`--authorized-users-path`
and `--rsa-private-key-path`, see `man iperf3`) instead of an address filter.

## Step 2 — Point the rig at it

In `config/config.yaml`:

```yaml
iperf3:
  enabled: true
  server: 203.0.113.45        # or a hostname
  port: 5201
  duration_s: 5
  interval_s: 60
  bitrate: 25M                # null = uncapped; see the warning above
  direction: download         # download | upload | both
  streams: 1
  run_data_budget_mb: 2000
```

Restart: `sudo systemctl restart viabot-survey`.

Then, on the dashboard, open **Subsystems** — `iperf3` should read `running`
with your server's address — and press **Run throughput test** to confirm the
round trip before relying on it.

## Choosing a direction

`download` is usually what matters: it is what a robot pulling a map, a model
update or a video stream experiences, and `-R` makes the server send.

`upload` matters if the robot pushes telemetry or video *out*. Cellular uplink
is typically far weaker than downlink and fails first at the edge of coverage,
so if the robot uploads anything substantial, measure it — but note that
`both` doubles the data spent per cycle.

## Interpreting the results

Results land in the `throughput` table and in the run report, not in the 1 Hz
sample stream — they are sparse by nature.

A failed test is data too: `error` is recorded alongside the timestamp, so a
stretch where iperf3 could not even connect lines up with the same stretch in
the ping trace and in the video.
