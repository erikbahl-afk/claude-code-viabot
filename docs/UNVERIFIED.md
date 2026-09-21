# What is verified, and what is not

No Claude session has ever had SSH access to the Pi or admin access to the
router. Every command output in this repository came from Erik running something
and pasting the result back. A number of things this software depends on are
expectations rather than facts.

`scripts/preflight.sh` answers most hardware questions in one pass. Run it and
paste the output before trusting anything below.

For defects in the code as opposed to gaps in knowledge, see the open list in
[CODE-TOUR.md](CODE-TOUR.md#things-that-are-wrong-right-now).

## Settled

### The rig works end to end (2026-09-11)

A walk of the office, with nobody pressing anything during it:

| | |
|---|---|
| Runnable | 85.7% of 314 s walked |
| Dead zones | 1, lasting 45 s at 100% loss (an antenna unplugged deliberately) |
| Latency | 30 / 46 / 205 ms min / median / max |
| Clip | 8.5 MB, 60 s, opening at 14:17:06, ten seconds before the outage |

The burned-in clock at the start of the clip matched the moment the link
dropped. That is the whole premise of the rig: a bad measurement resolves to the
right moment of footage without anyone marking it.

The Pi lost power during the original wrap-up, so the result was recovered
afterwards via `POST /api/runs/<id>/analyse`. That path is proven too.

Two lessons from that power loss:

* The Pi has no RTC, so log timestamps after a reboot are wrong until NTP
  catches up. The journal's first entry read 14:15:01 while `uptime -s` said
  14:20:25, five minutes apart on the same boot. When reading logs around a
  power loss, trust `uptime -s`.
* The journal was not persistent, so the log of the moments before the power
  loss was gone. `setup.sh` now creates `/var/log/journal`.

Both database durability and interrupted-run recovery changed as a result.

### The platform

| | Finding |
|---|---|
| `wlan0` serves an access point | Settled. `setup_ap.sh` reported AP mode at 192.168.50.1, the network was joined from a phone, and the control page loaded. The earlier `unavailable` state was NetworkManager's Wi-Fi switch being off. The script now turns it on. |
| The captive portal fires | The phone showed "Sign in to ViaBot-Survey" and opened the page unprompted. |
| The camera records | Settled, after a bug. The first walk recorded nothing: ffmpeg rejected the overlay filtergraph and the worker restarted it in a loop. The filtergraph is now validated at startup against a synthetic source, so a future mistake degrades to recording without the clock instead of recording nothing. |
| Clips are cut | Verified against real footage, including a dead zone straddling two segment files. |
| NetworkManager manages the interfaces | Confirmed. `dhcpcd` not installed, `eth0` at 192.168.1.139 with the default route via 192.168.1.1. |
| `viabot` sudo | In the `sudo` group, but prompts for a password. It is not passwordless. |
| Camera access | Already in the `video` group, no re-login needed. |
| Timezone | Already `America/Los_Angeles`, clock NTP-synchronised. |
| Camera modes | MJPEG at 1920x1080, 1280x1024, 1280x720, 1024x768, 800x600, 640x480, 352x288, 320x240, 160x120, every one at 30 fps and only 30 fps. This is why `camera.capture_fps` defaults to null: asking a UVC device for an interval it does not advertise skews timestamps. |
| `/dev/video1` | Metadata only (`Device Caps: 0x04a00000`). `/dev/video0` is the capture node. |
| Disk and memory | 117 GB card, 107 GB free. 1.8 GB RAM. CPU 34.6 C at idle. |
| Power | `throttled=0x0`, clean, no undervoltage since boot. Measured in mains-adjacent conditions, not mid-walk. |
| Uplink | Router 0.4 ms. 8.8.8.8 at 40 to 57 ms, 0% loss. Egress address in T-Mobile space. |
| The modem | Quectel EP06-A, LTE Cat 6, not 5G whatever LuCI's "Protocol: 5G" label says. Next hop past the router is 192.168.225.1, the Quectel factory default, consistent with it doing its own NAT. |
| Signal metrics | Working 2026-09-12. `client: "at_ssh"` returned LTE band 12, cell 1452806, RSRP -100, RSRQ -12, SINR 11, RSSI -73 within seconds of a restart. The router has no modem API at all. Readings come from `AT+QENG="servingcell"` on `/dev/ttyUSB2` over SSH. See [ROUTER.md](ROUTER.md). |
| Publishing | Working, verified 2026-09-14. A finished run uploaded over cellular to `viabotsurveys.com` (Vultr Dallas) and read back over HTTPS: 8,239 bytes of report plus an 8,473,967-byte clip, exactly what was queued. The full walk video stayed held, as designed. |

### What a Formant session actually looks like (2026-09-12)

Measured from a live session, `webrtc_internals_dump`, 37-second window:

* Robot to operator: **645 kbit/s mean, 672 median, 744 p95, 764 peak**.
* Operator to robot: **64 kbit/s mean, 104 peak**.

One robot, one set of camera settings. This is not a Formant specification.

Formant carries everything over **WebRTC data channels**, not RTP media tracks.
A session dump contains no `inbound-rtp` or `outbound-rtp` at all. The five
channels are `heartbeat` (50 msg/s out), `stream.latest-try-once` (18 msg/s in,
this is the video), `stream.reliable`, `stream.latest-ttl` and
`stream.latest-reliable`. That is why the bitrate is invisible to the usual
video stats and to Chrome's task manager, and why it has to be read from the
candidate-pair byte-rate series.

The session relays through TURN. The succeeded candidate pair was `relay` via
54.244.51.63, Twilio's edge in AWS us-west-2, with a mean round trip of 116 ms.
Media is not peer-to-peer. The robot's real first hop is garage, carrier, Twilio
edge, which is the leg the rig's uplink test models.

The SIM's plan is **unlimited** (confirmed 2026-09-12), so data volume is not a
constraint. Time and link capacity still are.

### The enclosure does not overheat (2026-09-13)

Harbor Freight Apache 2800 ($29.99), interior 11.9 x 9 x 5.3 in, pick-and-pull
foam, components zip-tied through slits.

An hour fully assembled, case closed, with a survey running so the camera was
encoding:

| | |
|---|---|
| Start | 45.7 C |
| Peak | 57.4 C across 119 samples |
| Time at or above 70 C | none, not one sample |
| Throttling | none at any point, `get_throttled` stayed `0x0` |

The curve flattened rather than climbing: about +10 C over the first 40 minutes,
then roughly +1 C over the last 18. Against the Pi 4's 80 C soft limit that
leaves roughly 23 C of headroom. Vent holes are not needed.

Two cautions. It was measured at room ambient, and a garage on a hot afternoon
starts higher with the peak rising roughly one for one. And it says nothing
about the modem, which has no sensor the Pi can read and works hardest exactly
where signal is weak.

The CPU clock dipping to 900 to 1200 MHz is not throttling. It appears
throughout the log and looks alarming, but `get_throttled` was zero every time.
That is the ondemand governor dropping the clock because the work was not there.

The power splice also held for the full hour with no undervoltage flag, which is
the longest continuous run anyone has measured it over.

### Other things safe to rely on

* 12.1 to 12.4 V at the router's input jack, across two separate checks.
* The Pi boots and is reachable at `garage-surveyor-01.local`. Note the "-or-".
* The router passes traffic. The Pi reaches 8.8.8.8 and resolves google.com at
  0% loss, 48 to 56 ms. Registration is LTE band 12 on T-Mobile (310/260), on a
  5 MHz carrier.
* The camera enumerates as `LRCP USB2.0` on `/dev/video0` and works on USB 2.0.
  The USB 3.0 assumption was wrong.
* Both antennas are threaded onto the router, not merely resting.
* There is no Y-splitter in the power chain and no soldered joint anywhere in
  it. See [HARDWARE.md](HARDWARE.md#the-splice).

## Still open

**What "good coverage" means for the robot.** No latency ceiling, throughput
floor, packet-loss tolerance or specific failure mode has ever been established.
The thresholds that colour the dashboard and the ones that decide dead zones are
invented. They are marked `provisional: true` and captioned as guesses in the
UI. The plan is one real survey walk, then set them from what the data looks
like.

**How much uplink teleop really needs.** The rig's rate comes from one
37-second capture of one robot: 645 kbit/s mean, 764 peak, steady around 650 to
700 with a lid on it, which reads like a configured encoder target rather than a
link being squeezed. The dump carries no `availableOutgoingBitrate` to prove
that. Against it, 10 Mbit/s down and 5 up has been quoted from memory with no
source. Both can be true, since the second has the shape of a provisioning
recommendation and would be right for more cameras or a higher resolution.

Raising the test rate does not settle it. The load runs alongside ping, and ping
is what dead zones are detected from, so offering 5 Mbit/s to a link that
carries 2 fills the queue and the garage reads as one long dead zone. Testing
high is safe only up to the point where the test becomes the failure.

What would settle it: the robot's configured camera bitrate from the Formant
console, and `./scripts/capacity_test.sh --udp 5M` at a weak spot to find out
whether 5 Mbit/s is even available to offer.

**Battery runtime.** The pack's Ah or Wh rating has never been read and no
runtime figure exists. Undervoltage is polled every second and stored per
sample, which detects a sagging supply but cannot predict how long the pack
lasts. Survey walks could get cut short with no warning.

**Which SMA port is MAIN and which is DIV.** Ports `a`, `b` and `c` were tested
one at a time on 2026-09-12. `a` and `c` each returned a working serving-cell
reading (RSRP -104, SINR 11 to 12) and `b` returned `ERROR`. Antennas are fitted
to `a` and `c` on that basis, which is reasonable, but the test could not
discriminate. Indoors the ambient signal is strong enough (RSSI -77) that a bare
connector couples plenty of RF, so every configuration looked alike and
consecutive rounds contradicted each other. Treat the assignment as unknown.

**Whether a second server build goes cleanly.** The first one works
(`viabotsurveys.com`, Vultr Dallas, 2026-09-14) but it took five fixes to
`server/setup.sh` to get there. The next box is the test of whether the script
is right.

**Whether a real garage produces sensible dead zones.** The thresholds have
never been checked against footage of a place anyone knows.

## Known limitations of the method

Not bugs. Consequences of what this rig is, worth stating so nobody discovers
them from a surprising number.

**A dead zone lasts longer than the dead spot does.** When the modem loses
signal it has to re-attach: scan, register, re-establish the data session. A
crude test on 2026-09-14, antennas wrapped in foil then unwrapped, took 10 to 15
seconds to recover. So a measured dead zone is the physical dead area plus the
recovery. Walk through a five-metre blind spot and the report may show twenty
seconds. That is arguably the honest number, since the robot cannot work during
re-acquisition either, but nobody should be surprised when a zone looks longer
than the place that caused it. One foil test is not a characterisation.

**Enabling the load test changes the headline percentage.** Ping shares the
modem's uplink buffer with it. Since 2026-09-18 the uplink load runs in short
bursts and those seconds are excluded from detection, so the percentage is
computed from roughly two thirds of the walk rather than all of it. The duty
cycle is regular and has nothing to do with where you are, so it is an unbiased
sample, but it is a sample. A short dead spot falling entirely inside a burst
will be missed. Runs from before and after are not comparable.

**What the exclusion does not fix.** Bufferbloat saturates. Once the buffer is
full, latency stops rising, so a link slightly short of the offered rate and one
hopelessly short look identical. The report counts seconds that delivered less
than 85% of what was offered and says those readings are a floor rather than a
measurement, but it cannot recover the number that would have been there. The
only real fix is to offer a rate the link can carry.

**The survey has already invented its own dead zones.** A run on 2026-09-17 at a
spot with good reception came back 42.3% runnable, median round trip 1381 ms
against a 32 ms base. Working back from that gives a modem buffer of about
1.75 Mbit (219 KB) and a link carrying roughly 1.3 Mbit/s, so a continuous
3 Mbit/s offer filled the buffer in about a second and kept it full for the
whole walk. Fill time scales inversely with the overshoot: 3M into 1.3M is one
second, 750k into 700k is thirty-five. The burst schedule bounds the damage
rather than removing it, and the rate is still unvalidated.

**This link's uplink swings by more than a factor of two at one spot.** At the
same place on 2026-09-18: a survey at 22:13 the night before carried 1.3 Mbit/s
with 57% loss at RSRP -110, a capacity test the next day passed a clean
3 Mbit/s, and a TCP ceiling test measured 1.64 Mbit/s with 147 retransmits. So a
fixed survey load sits comfortably inside capacity sometimes and well outside it
at others. The load rate question is not only "what does teleop need", it is
also "what can this link carry right now", which changes minute to minute.

**The radio score's bands are conventions, not requirements.** Poor, fair, good
and excellent are cut at the figures the industry generally uses for LTE
(RSRP -120 to -70 dBm, SINR -5 to 25 dB). Nothing has checked them against what
this robot needs, exactly like the dead-zone thresholds. The survey does measure
usability directly on the same seconds, so one real walk can finally test the
assumption underneath the score: does the radio score predict whether teleop
actually works? If it does not, the bands are wrong, not the link.

**The throughput plot is not a speed test.** The rig holds a UDP stream open at
the bitrate a teleop session uses and records what arrived each second, so the
plot's ceiling is the rate that was offered, not what the link could have
carried. A flat trace means the stream got through, not that this is all the
link can do. To measure capacity, `./scripts/capacity_test.sh` exists and
refuses to run during a walk, because saturating the link would manufacture
exactly the loss and latency the dead-zone detection reads.

**The percentage is only as good as the operator's discipline.** It is a share
of time, not of floor area, so it holds only if the walk is at a steady pace and
paused whenever standing still. A customer who does not pause while chatting in
a good spot will inflate the result, and nothing in the software can detect
that. There is no positioning and no motion sensor.

**Nothing says which level a dead zone was on.** Correlation is by timestamp to
video, so the level is whatever the footage shows. Manual marking was explicitly
rejected and this is the cost of that.

**Nothing prunes old surveys.** A 30-minute walk leaves roughly 250 MB of video,
so about 400 walks fill the card. The failure is at least loud: the camera
refuses to record below its disk floor and says so on the dashboard.
`DELETE /api/runs/<id>` removes a run's video, clips and report along with its
rows.

**A clock step mid-run would corrupt the timeline.** The Pi has no RTC. The run
start warns when the clock is unsynchronised, but nothing watches for NTP
stepping it during a walk. A backward step would put samples out of order and
break video correlation for everything after it.

**The rig carries plaintext secrets in public.** `config/config.yaml` holds the
Wi-Fi passphrase, the router password and, once publishing is on, the upload
token and the iperf3 password. The card is not encrypted and the rig is carried
through public car parks. Treat a lost rig as all of those being disclosed, and
rotate them.

## Load figures before 2026-09-17 are absent, not zero

The load test had never measured anything on this rig, and the reason was not
credentials. The rig runs iperf3 3.18 and the Dallas server runs 3.16, either
side of the 3.17 change from PKCS#1 to OAEP credential encryption, so every test
was rejected as an authorization failure while the password, the key and the
clock were all provably correct. The worker only reported "the server returned
no readings", so nothing distinguished a bad spot from a rejected login. Both
are now named, and the client falls back to the older padding.

The first real uplink measurement, on 2026-09-17, was 3.22 Mbit/s received over
3 seconds from the bench on the house router's cellular link. That is below the
5 Mbit/s quoted for teleop and well above the 645 kbit/s measured from a real
session, but it is one 3-second test from a spot that is not a garage.

## What the project is actually for

Worth writing down, because it changes what counts as a good measurement.

* The rig exists to decide whether a location can support robot teleoperation
  through Formant.io, with human operators in California and India.
* Formant's path is WebRTC, so UDP. Carriers shape UDP differently from TCP and
  drop it first under contention, which is why a TCP speed test can pass
  somewhere teleop will not work. Hence the UDP load test.
* Twilio's network diagnostic tool was investigated and rejected for four
  independent reasons: it is a browser tool needing Twilio NTS credentials that
  Formant customers do not get, headless Chromium on ARM64 has documented WebRTC
  problems, it has no API or scripted mode, and it would only ever test the hop
  to the nearest Twilio edge rather than the path to a distant operator.
* The garages in scope are in Florida, San Diego, the Bay Area, El Paso, North
  Carolina and Virginia, which is the argument for one central test server
  rather than a local one.

Nobody has yet said what "good enough" means numerically. The dead-zone
thresholds remain invented.
