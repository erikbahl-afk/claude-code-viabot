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

The point worth being careful about: **your phone's Wi-Fi is the control
channel, not the thing being measured.** Measurement traffic leaves `eth0`,
crosses the router, and goes out the modem. Those are two entirely separate
networks on the Pi:

| | Subnet | Role |
|---|---|---|
| `wlan0` | 192.168.50.0/24 | Access point. Serves the dashboard to your phone. No route to the internet. |
| `eth0` | 192.168.1.0/24 (DHCP from the router) | The link under test. Carries the default route. |

`ping` is explicitly pinned to `eth0` with `-I`, so a future routing change
cannot quietly make the rig measure the wrong interface.

AP clients are blocked from being routed out to the modem by an nftables rule
(`ap.block_client_internet`, on by default). Two reasons: your phone's
background traffic would otherwise share — and distort — the very link being
measured, and a Wi-Fi network with no internet is what makes phones reliably
open the captive portal instead of silently falling back to LTE.

## The captive portal

Phones decide whether a Wi-Fi network has internet by fetching a known URL and
checking for a specific response — Android wants HTTP 204 from
`connectivitycheck.gstatic.com/generate_204`, iOS wants a page containing
`Success` from `captive.apple.com`, and so on. If the answer is wrong, the phone
concludes it is behind a captive portal and opens the page in a webview.

We exploit that deliberately:

1. NetworkManager runs the AP in `shared` mode, which starts a dnsmasq bound to
   `wlan0` for DHCP and DNS.
2. A drop-in at `/etc/NetworkManager/dnsmasq-shared.d/viabot-captive.conf`
   contains `address=/#/192.168.50.1`, so **every** DNS name a client looks up
   resolves to the Pi. (This affects only the AP's dnsmasq; the Pi's own name
   resolution over `eth0` is untouched.)
3. The probe therefore arrives at our Flask app, which answers with a 302 to
   `http://192.168.50.1/` — a redirect carrying an HTML body with a link, since
   some portal agents render the body rather than following the redirect.
4. The same drop-in also sets DHCP option 114 (RFC 8910), which modern iOS and
   Android read directly, skipping the probe dance.

The app treats a request as a probe whenever its `Host` header is not one of
ours (see `_build_allowed_hosts`), plus a hard-coded list of probe paths for
clients that ask our own address directly.

**Caveat.** Captive-portal webviews are cut-down browsers. The dashboard is
written to survive that — no modules, no external libraries, no CDNs, plain
`fetch` polling rather than WebSockets or SSE — but if it ever behaves oddly,
open `http://192.168.50.1/` in Safari or Chrome instead. Everything works
there.

## Processes

One systemd service (`viabot-survey.service`) runs everything as the `viabot`
user. It gets `CAP_NET_BIND_SERVICE` so it can hold port 80 without being root
— the portal only works on port 80.

Inside it:

| Thread | Does |
|---|---|
| waitress (8 threads) | Serves the dashboard and API |
| `sampler` | Once a second: reads every worker, classifies, writes a row |
| `ping` | Streams `ping -O -D` output into a rolling window |
| `dns` | Resolves a hostname every 30 s |
| `router` | Polls the modem for RSRP/SINR (when configured) |
| `iperf3` | Runs throughput tests (when configured) |
| `camera` | Supervises the ffmpeg recording process |

Every worker subclasses `workers.base.Worker`, which restarts it with capped
exponential backoff if it dies. Unplugging the camera mid-walk degrades that one
subsystem; it does not end the run.

The workers run for the whole life of the service, not just during a run. That
way the dashboard shows live link quality the moment you connect — you can
confirm the rig is healthy *before* walking into the garage. Starting a run only
begins *recording*.

## How a timestamp becomes a video frame

This is the mechanism the whole rig exists for.

1. ffmpeg writes segments with `-strftime 1`, so each file is named for the wall
   clock at which it started: `20260911-143005.mkv`.
2. `CameraWorker.locate(ts)` finds the newest segment whose start is at or
   before `ts`, and returns `(filename, ts - start)`.
3. Every sample and every mark stores that pair at the moment it is recorded.
4. The run report groups contiguous bad samples into problem areas and carries
   the pair forward, so the report says "open `20260911-143005.mkv` at 123 s".

In `overlay` mode the clock is also burned into the picture, derived from each
frame's PTS plus the capture start time — not from render time, so the label
cannot drift if encoding falls behind capture.

**All of this depends on the system clock.** The Pi has no real-time clock; it
learns the time from NTP over the cellular link after boot. `clock_synced` is
shown on the dashboard, stored on every sample, and logged as a warning at the
start of any run that begins unsynchronised.

## Data model

SQLite at `data/surveys.db`, WAL mode so the web threads read while the sampler
writes.

| Table | Rate | Holds |
|---|---|---|
| `runs` | per walk | id, start/end, label, the config in force, the git commit |
| `samples` | 1 Hz | RTT, loss, jitter, status, DNS, modem fields, video pointer, clock-sync flag |
| `marks` | on tap | timestamp, category, note, status at that moment, video pointer |
| `throughput` | sparse | iperf3 results and bytes spent |
| `events` | sparse | worker warnings and errors, shown on the dashboard |

Throughput and marks are separate tables precisely because they are sparse —
it keeps the once-a-second row narrow.

## Classification

`runner.classify()` turns a ping reading into the colour on the banner:

| Status | When |
|---|---|
| `good` | below every threshold |
| `degraded` | loss ≥ 5 % or RTT ≥ 200 ms |
| `bad` | loss ≥ 20 % or RTT ≥ 500 ms, or total loss briefly |
| `dead` | 100 % loss sustained for ≥ 5 s |
| `unknown` | no data at all |

All five numbers are configurable under `thresholds:`. The distinction between
`bad` and `dead` matters: walking past a pillar drops a packet or two, and that
is not a dead zone.

## Updates

`updater.py` shells out to git. Applying an update cannot be done in-process —
the restart would kill the request that asked for it — so the app starts
`viabot-update.service` (a oneshot running `scripts/update.sh`) through two
narrowly scoped `sudo` grants installed in `/etc/sudoers.d/viabot-survey`. The
browser then polls `/api/health` until the service answers again.

## Deliberate non-goals

- **No authentication.** The Wi-Fi passphrase is the only lock. One operator,
  one rig, physically present.
- **No video over Wi-Fi.** Streaming 720p over the AP while measuring is a
  distraction; video is collected over the wired LAN afterwards.
- **No GPS.** It does not work in a concrete garage. Visual correlation plus
  operator marks is the localisation strategy, and it is the more reliable one.
- **No GPIO button.** The phone in your hand is a better button, and it needs no
  hardware that has not been bought yet.
