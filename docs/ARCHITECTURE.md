# Architecture

## The physical setup

```
   ┌──────────┐      ┌───────────┐   12V   ┌──────────────────────────┐
   │ Battery  ├─────►│  DC-DC    ├────────►│ Router  (OpenWrt HL7621) │
   │ (fleet   │      │  board    │  Out 1  │                          │
   │  spare)  │      │ (ViaBot)  │         │  internal 5G modem ──────┼──►  ~~ cellular ~~ ► internet
   └──────────┘      └─────┬─────┘         │  usb0 = WAN              │
                           │ Out 2         │                          │
                           │  12V          │  LAN 1 ◄──┐   LAN 2 (free,│
                           ▼               └───────────┼──┬───────────┘
                 ┌───────────────────┐                 │  │
                 │ 12ft cable ► USB  │                 │  └── admin laptop, for
                 │ charger ► USB-C   │                 │      collecting data
                 └─────────┬─────────┘                 │
                           ▼                           │ Ethernet
                 ┌──────────────────────────────────┐  │
                 │ Raspberry Pi 4B                  │  │
                 │  eth0  ◄─────────────────────────┼──┘
                 │  wlan0 ── Wi-Fi AP 192.168.50.1  │◄···· your phone
                 │  USB   ── camera /dev/video0     │
                 └──────────────────────────────────┘
```

The phone's Wi-Fi is the control channel, not the thing being measured.
Measurement traffic leaves `eth0`, crosses the router, and goes out the modem.
Those are two separate networks on the Pi:

| | Subnet | Role |
|---|---|---|
| `wlan0` | 192.168.50.0/24 | Access point. Serves the dashboard. No route to the internet. |
| `eth0` | 192.168.1.0/24 (DHCP from the router) | The link under test. Carries the default route. |

`ping` is pinned to `eth0` with `-I` so a future routing change cannot quietly
make the rig measure the wrong interface. iperf3 needs the same pinning but
takes an address rather than an interface name, so `udpload.py` resolves it
first.

An nftables rule blocks AP clients from routing out to the modem
(`ap.block_client_internet`, on by default). Two reasons. Your phone's
background traffic would otherwise share and distort the link being measured,
and a Wi-Fi network with no internet is what makes phones open the captive
portal instead of silently falling back to LTE.

## The captive portal

Phones decide whether a network has internet by fetching a known URL and
checking the response. Android wants HTTP 204 from
`connectivitycheck.gstatic.com/generate_204`, iOS wants a page containing
`Success` from `captive.apple.com`. If the answer is wrong, the phone assumes a
captive portal and opens the page in a webview.

That is exploited deliberately:

1. NetworkManager runs the AP in `shared` mode, which starts a dnsmasq bound to
   `wlan0` for DHCP and DNS.
2. A drop-in at `/etc/NetworkManager/dnsmasq-shared.d/viabot-captive.conf`
   contains `address=/#/192.168.50.1`, so every DNS name a client looks up
   resolves to the Pi. This affects only the AP's dnsmasq. The Pi's own
   resolution over `eth0` is untouched.
3. The probe arrives at the Flask app, which answers with a 302 to
   `http://192.168.50.1/`. The redirect carries an HTML body with a link,
   because some portal agents render the body instead of following the redirect.
4. The same drop-in sets DHCP option 114 (RFC 8910), which modern iOS and
   Android read directly and skip the probe dance.

The app treats a request as a probe when its `Host` header is not one of ours
(`_build_allowed_hosts`), plus a hard-coded list of probe paths for clients that
ask our address directly.

Captive-portal webviews are cut-down browsers. The dashboard is written to
survive that: no modules, no libraries, no CDNs, plain `fetch` polling instead of
WebSockets or SSE. If it ever behaves oddly, open `http://192.168.50.1/` in
Safari or Chrome.

## Processes

One systemd service (`viabot-survey.service`) runs everything as the `viabot`
user. It gets `CAP_NET_BIND_SERVICE` so it can hold port 80 without being root,
and the portal only works on port 80.

Inside it:

| Thread | Does |
|---|---|
| waitress (8 threads) | Serves the dashboard and API |
| `sampler` | Once a second: reads every worker, classifies, writes a row |
| `ping` | Streams `ping -O -D` output into a rolling window |
| `dns` | Resolves a hostname every 30 s |
| `router` | Polls the modem for RSRP and SINR |
| `udp_up` / `udp_down` | The teleop-sized UDP load test, one worker per direction |
| `camera` | Supervises the ffmpeg recording process |
| `publisher` | Uploads finished reports and clips, idle during a run |

Every worker subclasses `workers.base.Worker`, which restarts it with capped
exponential backoff if it dies. Unplugging the camera mid-walk degrades that one
subsystem without ending the run.

Most workers run for the whole life of the service rather than just during a
run, so the dashboard shows live link quality the moment you connect and you can
confirm the rig is healthy before walking into the garage. The camera and the
load tests are the exceptions: they start and stop with the run, because the
load test is a real load on the link being measured and leaving it running
between walks spends the link for nothing.

## How a timestamp becomes a video frame

This is the mechanism the rig exists for.

1. ffmpeg writes segments with `-strftime 1`, so each file is named for the wall
   clock at which it started: `20260911-143005.mkv`.
2. `CameraWorker.locate(ts)` finds the newest segment starting at or before
   `ts`, and returns `(filename, ts - start)`.
3. Every sample stores that pair when it is recorded.
4. At the end of the run, `deadzones.py` groups unusable samples into zones and
   cuts a clip for each: `pre_roll` seconds of approach, the zone itself, then
   `post_roll` seconds of recovery. Cutting uses stream copy, so it is fast, and
   the cost is that a clip starts at the nearest keyframe. The pre-roll absorbs
   that. A zone near a segment boundary is served by concatenating two files
   first.

In `overlay` mode the clock is burned into the picture, derived from each
frame's PTS plus the capture start time rather than from render time, so the
label cannot drift if encoding falls behind capture.

All of this depends on the system clock, and the Pi has no real-time clock. It
learns the time from NTP over the cellular link after boot. `clock_synced` is
shown on the dashboard, stored on every sample, and logged as a warning at the
start of any run that begins unsynchronised.

## Data model

SQLite at `data/surveys.db`. WAL mode so the web threads can read while the
sampler writes, and `synchronous=FULL` because this rig loses power for real.

| Table | Rate | Holds |
|---|---|---|
| `runs` | per walk | id, start and end, label, the config in force, the git commit |
| `samples` | 1 Hz | RTT, loss, jitter, status, DNS, modem fields, load-test fields, video pointer, clock-sync flag, whether the rig was loading the uplink |
| `dead_zones` | at run end | start, end, duration, worst loss and latency, clip path |
| `throughput` | sparse | iperf3 results and bytes spent |
| `uploads` | sparse | the publish queue, with per-chunk progress |
| `events` | sparse | worker warnings and errors, shown on the dashboard |

Throughput, uploads and dead zones are separate tables because they are sparse,
which keeps the once-a-second row narrow. Dead zones are stored rather than
recomputed on read, because the clips on disk are tied to them. A finished run
can still be re-analysed with different thresholds, which replaces the stored set
rather than adding to it.

New sample columns go in both `SAMPLE_COLUMNS` and `ADDED_COLUMNS`. The second
is what lets a rig that has been running for weeks pick up the column instead of
failing every insert.

## Classification

`runner.classify()` turns a ping reading into the colour on the banner:

| Status | When |
|---|---|
| `good` | below every threshold |
| `degraded` | loss at or above 5%, or RTT at or above 200 ms |
| `bad` | loss at or above 20%, or RTT at or above 500 ms, or brief total loss |
| `dead` | 100% loss sustained for 5 s or more |
| `unknown` | no data at all |

All five numbers are configurable under `thresholds:`. The distinction between
`bad` and `dead` matters: walking past a pillar drops a packet or two, and that
is not a dead zone.

Note that these thresholds only colour the live banner. What lands in the report
is decided separately, under `deadzone:`.

## Updates

`updater.py` shells out to git to check for and fetch changes. Applying an update
cannot be done in-process, because the restart would kill the request that asked
for it.

It also cannot be done with `sudo`. The service runs with a
`CapabilityBoundingSet` of `CAP_NET_BIND_SERVICE`, and a bounding set without
`CAP_SETUID` and `CAP_SETGID` makes sudo fail outright. The same command from a
login shell works fine, which is why this took three attempts to diagnose: every
manual test over SSH passed while the button did nothing.

So the app asks for an update by creating `<data_dir>/update-requested`, and
`viabot-update.path` notices the file and starts `viabot-update.service`, which
runs `scripts/update.sh`. Anything else the app needs from systemd has to go the
same way.

The browser then polls `/api/health` and waits for `started_at` to *change*. The
old process answers that endpoint perfectly happily throughout the update, so
waiting for a successful reply lands you back on the old version.

## Deliberate non-goals

* **No authentication.** The Wi-Fi passphrase is the only lock. One operator,
  one rig, physically present.
* **No video over Wi-Fi.** Streaming 720p over the AP while measuring is a
  distraction. Video is collected over the wired LAN or uploaded after the walk.
* **No GPS.** It does not work in a concrete garage. The video is the
  localisation: you recognise the place by looking at it.
* **No marking of any kind.** The rig detects dead zones itself, so there is
  nothing to press while walking and no GPIO button to source. The operator's
  hands stay free.
* **No results on the phone** beyond a single percentage. Reading a report on a
  4-inch screen in a car park is worse than reading it on a laptop afterwards.

## Why the percentage is a percentage of time

There is no indoor positioning, so the rig cannot know how much ground was
covered, only how many seconds elapsed. Walk slowly through a dead zone and it
looks worse than it is. Walk briskly and it looks better.

Two things keep it honest. Pause stops measuring and recording together, so time
spent standing still is absent from the data rather than counted as coverage.
And the per-zone detail, how many and how long and the footage of each, is what
you act on. The percentage is the number you report to someone else.

Since 2026-09-18 there is a third wrinkle: seconds when the rig was loading the
uplink itself are excluded too, for the reasons in
[CODE-TOUR.md](CODE-TOUR.md#excluded-seconds). The report prints both the walked
time and the judged time so the difference is visible.
