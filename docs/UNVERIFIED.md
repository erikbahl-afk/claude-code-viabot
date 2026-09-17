# What has never been checked on the real hardware

This rig was built and documented across two earlier sessions, neither of which
had SSH access to the Pi or admin access to the router — every command output in
those handoffs came from Erik running commands and pasting results back. The
current session has no route to the rig's LAN either.

So a number of things this software depends on are **expectations, not facts**.
`scripts/preflight.sh` answers all of them in one pass. Run it and paste the
output before trusting any of the below.

## Proven end to end, 2026-09-11

A walk of the office produced, with nobody pressing anything during it:

| | |
|---|---|
| Runnable | 85.7% of 314 s walked |
| Dead zones | 1, lasting 45 s at 100% loss (an antenna unplugged deliberately) |
| Latency | 30 / 46 / 205 ms min / median / max |
| Clip | 8.5 MB, 60 s, opening at 14:17:06 — ten seconds before the outage |

The burned-in clock at the start of the clip matched the moment the link
dropped, which is the whole premise of the rig: a bad measurement resolves to
the right moment of footage without anyone marking it.

The result was recovered *after* the fact via `POST /api/runs/<id>/analyse`,
because the Pi lost power during the original wrap-up. That path is therefore
also proven.

## Established by the first real walk, 2026-09-11

The rig recorded a walk, detected a dead zone (an antenna unplugged for 45 s),
picked the right video and offset, and began cutting a clip. Then the Pi lost
power mid-cut, which exposed two things worth keeping in mind:

- **The Pi has no RTC, so log timestamps after a reboot are wrong until NTP
  catches up.** In this case the journal's first entry read 14:15:01 while
  `uptime -s` said 14:20:25 — five minutes apart, same boot. When reading logs
  around a power loss, trust `uptime -s` over the stamps.
- **The journal was not persistent**, so the log of the moments before the
  power loss was gone. `setup.sh` now creates `/var/log/journal`.

Both database durability and interrupted-run recovery were changed as a result;
see the commit for 2026-09-11.

## Still open

| | Status | If the assumption is wrong |
|---|---|---|
| **Power headroom** | The battery's Ah/Wh rating has never been read and no runtime figure exists. Undervoltage flags are polled every second and stored per sample — which detects a sagging supply but cannot predict how long the pack lasts. | Survey walks get cut short with no warning. |

## Answered by the first real setup, 2026-09-11

| | Finding |
|---|---|
| **`wlan0` serves an access point** | **Settled.** `setup_ap.sh` reported `wlan0 is in AP mode` at 192.168.50.1, the network was joined from a phone, and the control page loaded. The earlier `unavailable` state was NetworkManager's own Wi-Fi switch being off; the script now turns it on itself. |
| **The captive portal fires** | The phone showed "Sign in to ViaBot-Survey" and opened the page unprompted. |
| **The camera records** | **Settled, after a bug.** The first walk recorded nothing: ffmpeg rejected the overlay filtergraph and the worker restarted it in a loop. Fixed, and the filtergraph is now validated at startup against a synthetic source, so a future mistake here degrades to recording without the clock instead of recording nothing. |
| **Clips are cut** | Verified end to end against real footage, including the case where a dead zone straddles two segment files and they have to be joined. |
| **The rest of the chain** | A run started, paused, resumed, ended, and reported a percentage. Link, power, disk and clock all read healthy. |

## Answered by the preflight run of 2026-09-11

| | Finding |
|---|---|
| **NetworkManager manages the interfaces** | Confirmed. `NetworkManager` active, `dhcpcd` not installed, `eth0` connected at 192.168.1.139 with the default route via 192.168.1.1. |
| **`viabot` sudo** | In the `sudo` group, but **sudo prompts for a password** — it is not passwordless. `setup.sh` handles this; `update.sh` relies on the narrow NOPASSWD rules that `setup.sh` installs. |
| **`viabot` camera access** | Already in the `video` group, so no re-login is needed. |
| **Timezone** | Already set to `America/Los_Angeles` (PDT, −0700), clock NTP-synchronised. Better than assumed. |
| **Camera capabilities** | In MJPEG: 1920×1080, 1280×1024, 1280×720, 1024×768, 800×600, 640×480, 352×288, 320×240, 160×120 — every one at **30 fps and only 30 fps**. This is why `camera.capture_fps` defaults to null: asking a UVC device for an interval it does not advertise skews timestamps. |
| **`/dev/video1`** | Confirmed metadata-only (`Device Caps: 0x04a00000`). `/dev/video0` is the capture node. |
| **Disk and memory** | 117 GB card, 107 GB free. 1.8 GB RAM. CPU 34.6 °C at idle. |
| **Power** | `throttled=0x0` — clean, no undervoltage since boot. Measured on mains-adjacent conditions, not mid-walk. |
| **Uplink** | Router 0.4 ms; 8.8.8.8 at 40–57 ms, 0% loss. Egress address is in T-Mobile space. |
| **The modem** | A **Quectel EP06-A** — LTE Cat 6, *not* 5G, whatever LuCI's "Protocol: 5G" interface label says. The next hop past the router is 192.168.225.1, the Quectel factory default, consistent with it doing its own NAT. |
| **Publishing, end to end** | **Working, verified 2026-09-14.** A finished run was queued on the rig, uploaded over cellular to `viabotsurveys.com` (Vultr Dallas, vc2-1c-1gb), and read back over HTTPS in a browser: 8,239 bytes of report plus an 8,473,967-byte clip, which is exactly what was queued. The full walk video stayed held, as designed. |
| **Teleop bitrate** | **Measured 2026-09-12** from a live session (`webrtc_internals_dump`, 37 s window): robot → operator **645 kbit/s mean, 672 median, 744 p95, 764 peak**; operator → robot **64 mean, 104 peak**. Hence `uplink_bitrate: 1M`, `downlink_bitrate: 300k`. One robot, one set of camera settings — not a Formant specification. |
| **How Formant carries media** | Over **WebRTC data channels**, not RTP media tracks. A session dump contains no `inbound-rtp`/`outbound-rtp` at all; the five channels are `heartbeat` (50 msg/s out), `stream.latest-try-once` (18 msg/s in — this is the video), `stream.reliable`, `stream.latest-ttl`, `stream.latest-reliable`. This is why the bitrate is invisible to the usual video stats and to Chrome's task manager, and why it has to be read from the candidate-pair byte-rate series. |
| **The session relays through TURN** | The succeeded candidate pair was `relay` via **54.244.51.63** — Twilio's TURN edge, in AWS us-west-2 — with a mean round trip of **116 ms**. Media is not peer-to-peer. The robot's real first hop is therefore garage → carrier → Twilio edge, which is the leg the rig's uplink test models. |
| **The SIM's data plan** | **Unlimited** (Erik, 2026-09-12). Data volume is therefore not a constraint on what the rig measures. Time and link capacity still are: a 250 MB video upload over a weak cellular link takes as long as it takes. |
| **Signal metrics** | Working on the rig, verified 2026-09-12: `client: "at_ssh"` returned LTE band 12, cell 1452806, RSRP −100, RSRQ −12, SINR 11, RSSI −73 within seconds of a restart. The router has no modem API at all — `/ubus` 404s, there is no LuCI RPC, and `ubus list` carries no modem object. The readings come from `AT+QENG="servingcell"` on `/dev/ttyUSB2`, over SSH from the Pi. `AT+QRSRP` is unsupported on this firmware. See [ROUTER.md](ROUTER.md). |

## Never established at all

**What "good coverage" means for the robot.** No latency ceiling, throughput
floor, packet-loss tolerance, or specific robot failure mode has ever been
discussed — the prior sessions were entirely about physical assembly. The
thresholds that colour the dashboard are therefore invented. They are marked
`provisional: true` in the config and captioned as guesses in the UI. The plan
is to do one real survey walk and set them from what the data actually looks
like.

**Which SMA port on the field router is MAIN and which is DIV.** Ports were
labelled `a`, `b` and `c` and tested one at a time on 2026-09-12. `a` and `c`
each returned a working serving-cell reading (RSRP −104, SINR 11–12) and `b`
returned `ERROR`; a+c was also the fastest throughput pair. Antennas are fitted
to `a` and `c` on that basis, which is a reasonable call — but the test could
not discriminate. Indoors the ambient signal is strong (RSSI −77) and a bare
connector couples enough RF for the modem to camp regardless, so every
configuration looked alike and consecutive rounds contradicted each other.
Treat the MAIN/DIV assignment as unknown.

**How many levels each garage has.**

## Known limitations of the method

Not bugs — consequences of what this rig is, worth stating so nobody discovers
them from a surprising number.

**A dead zone lasts longer than the dead spot does.** When the modem loses
signal it has to re-attach when it comes back — scan, register, re-establish the
data session — and that takes time. A crude test on 2026-09-14 (antennas wrapped
in foil, then unwrapped) took **10–15 seconds** to recover.

So a measured dead zone is the physical dead area *plus* the recovery. Walk
through a five-metre blind spot and the report may show twenty seconds. That is
arguably the honest number — the robot cannot work during re-acquisition either —
but nobody should be surprised when a zone looks longer than the place that
caused it.

The 10–15 s figure is one foil test, not a characterisation. A real garage will
say whether it holds.

**Turning on `udp_load` will change the headline percentage.** Ping runs
continuously and is what dead zones are detected from. With the load test
active, ping is measuring a link that is carrying a teleop-sized stream rather
than an idle one, so loss and latency will be worse and more dead zones will be
found — in the same garage. That is arguably the more honest number, but it
means **results from before and after enabling it are not comparable**, and the
thresholds were conceived for an idle link. Set thresholds after deciding
whether the load test is on, not before.

**The survey load is 3 Mbit/s up / 5 Mbit/s down as of 2026-09-17, and the
uplink figure is bounded by the rig, not by the robot.** The choice was between
645 kbit/s (measured from one real session) and 5 Mbit/s (quoted from memory,
unsourced). 3M splits them at ~4.6x the measurement — but the reason it is not
5M is this link's own uplink ceiling of 4.48 Mbit/s at a *good* spot. A
continuous load at the ceiling saturates the uplink for the whole walk, and
ping, which is what dead zones are detected from, shares it. The survey would
then find dead zones it created.

Consequences to hold on to: runs before and after this change are not
comparable; the provisional thresholds were conceived for an idle link and are
now further from it than ever; and the right way to settle the rate is still a
measurement of what a robot sends with every camera an operator would open.

**How much uplink teleop really needs is not settled.** The rig's rate is set
from one 37-second capture of one robot: 645 kbit/s mean, 764 peak, steady
around 650-700 with a lid on it — which reads like a configured encoder target
rather than a link being squeezed, though the dump carries no
`availableOutgoingBitrate` to prove it. Against that, 10 Mbit/s down / 5 up has
been quoted from memory, unsourced. Both can be true: the second is the shape of
a *provisioning recommendation*, and would also be right for more cameras or a
higher resolution than the one measured.

This is not settled by raising the test rate. The load is constant-rate UDP
running alongside ping, and ping is what dead zones are detected from — offer 5
Mbit/s to a link that carries 2 and the queue fills, latency spikes, and the
garage reads as one long dead zone. Testing high is the safe direction only up
to the point where the test becomes the failure.

What would settle it: the robot's configured camera bitrate from the Formant
console, and `./scripts/capacity_test.sh --udp 5M` at a weak spot to find out
whether 5 Mbit/s is even available to offer.

**The throughput plot is not a speed test, and the report says so.** The rig
holds a UDP stream open at the bitrate a teleop session really uses and records
what arrived each second. So the plot's ceiling is the rate that was *offered*
(1.5 Mbit/s up, 300 kbit/s down), not what the link could have carried, and a
flat trace means "the stream got through", not "that is all the link can do".
Nothing here measures capacity: the iperf3 burst worker that would is shipped
disabled on purpose, because saturating the link would manufacture exactly the
loss and latency the dead-zone detection reads.

**The percentage is only as good as the operator's discipline.** It is a share
of time, not of floor area, so it holds only if the walk is at a steady pace
and paused whenever standing still. A customer operating the rig who does not
pause while chatting in a good spot will inflate the result, and nothing in the
software can detect that — there is no positioning and no motion sensor.

**Nothing says which level a dead zone was on.** Correlation is by timestamp to
video, so the level is whatever the footage shows. Manual marking was
explicitly rejected, and this is the cost of that.

**Nothing prunes old surveys.** A 30-minute walk leaves roughly 250 MB of
video. The card is 107 GB, so around 400 walks fill it. The failure is at least
loud rather than silent: the camera refuses to record below its disk floor and
says so on the dashboard. `DELETE /api/runs/<id>` now removes a run's video,
clips and report along with its rows.

**A clock step mid-run would corrupt the timeline.** The Pi has no RTC. The run
start warns when the clock is unsynchronised, but nothing watches for NTP
stepping it *during* a walk. A backward step would put samples out of order and
break video correlation for everything after it.

**`udp_load` had never measured anything on this rig, and now the reason is
known.** Not credentials: an iperf3 version mismatch. The rig is 3.18 and the
Dallas server 3.16, either side of the 3.17 change from PKCS#1 to OAEP
credential encryption, so every test was rejected as an authorization failure
while the password, the key and the clock were all provably correct. Fixed by
falling back to the older padding. Load figures from before 2026-09-17 are
absent, not zero.

**First real uplink measurement, 2026-09-17:** 3.22 Mbit/s received over 3
seconds from the bench, on the house router's cellular link. That is *below*
the 5 Mbit/s that was quoted for teleop, and well above the 645 kbit/s measured
from a real session — but it is one 3-second test from one spot that is not a
garage. Re-measure with `./scripts/capacity_test.sh --udp 5M` somewhere weak
before drawing anything from it.

**`udp_load` may never have measured anything on this rig.** On 2026-09-17 the
capacity probe came back "test authorization failed" in both directions, which
is also what the load test would have been hitting silently — the worker only
reported "the server returned no readings", so nothing distinguished a bad spot
from a rejected login. Both are now named. Whether any walk ever carried a real
UDP load is unverified; treat load figures from before this date as absent
rather than as zero.

**The rig carries plaintext secrets in public.** `config/config.yaml` holds the
Wi-Fi passphrase, the router password, and — once publishing is on — the upload
token and the iperf3 password. The card is not encrypted and the rig is carried
through public car parks. Treat a lost rig as all of those being disclosed, and
rotate them.

## What the project is actually for

From the case-and-network addendum, and worth having written down because it
changes what counts as a good measurement:

- The rig exists to decide whether a location can support **robot teleoperation
  through Formant.io**, with human operators in **California and/or India**.
- Formant's path is **WebRTC, so UDP**. Carriers shape UDP differently from TCP
  and drop it first under contention, which is why a TCP speed test can pass
  somewhere teleop will not work. Hence `udp_load`.
- Twilio's network diagnostic tool was investigated as a way to test this and
  **rejected** for four independent reasons: it is a browser tool needing
  Twilio NTS credentials Formant customers do not get; headless Chromium on
  ARM64 has documented WebRTC problems; it has no API or scripted mode; and it
  would only ever test the hop to the nearest Twilio edge, not the path to a
  distant operator.
- The garages in scope are in **Florida, San Diego, the Bay Area, El Paso,
  North Carolina and Virginia** — which is the argument for one central test
  server rather than a local one.

Still nobody has said what "good enough" means numerically. The dead-zone
thresholds remain invented.

## The enclosure

Chosen: **Harbor Freight Apache 2800** ($29.99), interior 11.9 × 9 × 5.3 in,
pick-and-pull foam. Components secured by cutting slits in the foam and zip-tying
through them.

**Overheating is a live concern, not a hypothetical one.** Foam insulates, and a
closed case has no airflow. The two parts that matter are the cellular modem —
which works hardest and hottest exactly when signal is weak, which is the
condition the survey is there to characterise — and the Li-ion pack, where it is
a safety question rather than a performance one. The agreed plan: run unlatched
during a survey, treat closed-and-latched as transport only, route the foam
channels so the Pi's fan exhaust and the router's vents reach an opening, and
add vent holes with mesh only if needed.

**Validated 2026-09-13 — it does not overheat.** An hour with the rig fully
assembled, the case closed, and a survey run active so the camera was encoding:

| | |
|---|---|
| Start | 45.7 °C |
| Peak | **57.4 °C**, across 119 samples |
| Time at or above 70 °C | **none** — not one sample of 119 |
| Throttling | **none, at any point** — `get_throttled` stayed `0x0` for the whole hour |

The curve flattened rather than climbing: about +10 °C over the first 40 minutes,
then roughly +1 °C over the last 18, so it was settling near the high fifties
rather than still heading up. Against the Pi 4's 80 °C soft limit that leaves
**roughly 23 °C of headroom**, which is enough to absorb a garage a good deal
warmer than the room this was measured in. Vent holes are not needed.

Two cautions on reading that. It was measured at whatever the room's ambient was
— a garage on a hot afternoon starts higher, and the peak rises with it roughly
one-for-one. And it does not say anything about the **modem**, which has no
temperature sensor the Pi can read and which works hardest exactly where signal
is weak.

**The CPU clock dipping to 900–1200 MHz is not throttling.** It appears
throughout the log and looks alarming. `get_throttled` was zero every time, so
the cap was never thermal or electrical — that is the ondemand governor dropping
the clock because the work was not there to need it. Recording video in copy
mode leaves the Pi 4 mostly idle.

**Bonus result: the power splice held.** An hour under load with no undervoltage
flag at all. That splice is the part that has failed before, and this is the
longest continuous run anyone has measured it over.

## Things that are known

Verified in the original session and safe to rely on:

- 12.1–12.4 V at the router's input jack, across two separate checks.
- The Pi boots, is reachable over SSH at `garage-surveyor-01.local`, and its
  hostname is `garage-survey**or**-01`, not `garage-survey-01`.
- The router passes traffic; the Pi reaches `8.8.8.8` and resolves
  `google.com` at 0% loss, ~48–56 ms. The registration is LTE, not 5G —
  band 12 on T-Mobile (310/260), on a 5 MHz carrier.
- The camera enumerates as `LRCP USB2.0` on `/dev/video0`, works on **USB 2.0**
  (the USB 3.0 assumption was wrong), and produced a valid test image. Its full
  mode list is now known — see the table above.
- Both antennas are threaded onto the router, not merely resting.
- There is **no Y-splitter** in the power chain, and **no soldered joint**
  anywhere in it — see [HARDWARE.md](HARDWARE.md#the-splice).
