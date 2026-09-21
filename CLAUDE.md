# Notes for Claude sessions on this repository

## What this is

A Raspberry Pi rig carried through parking garages to find cellular dead zones,
with a camera recording simultaneously so each bad measurement can be tied to a
place you can actually look at. The operator controls it from a phone, over a
Wi-Fi access point the Pi itself broadcasts, through a captive portal.

Read `docs/ARCHITECTURE.md` before making structural changes.

## The user

Erik. New to development workflows and to git. He works through the Claude web
interface and applies changes by merging a pull request on github.com and
pressing "Apply update" on the rig's dashboard. See `docs/WORKFLOW.md`.

Consequences for how to work here:

- **Explain in terms of what he does, not what git does.** "Merge the PR, then
  press Apply update", not "rebase onto origin/main".
- **Don't leave him mid-surgery.** Anything that needs hand-editing a file over
  SSH is a failure mode; the next update discards it anyway. Changes belong in
  the repo.
- **Keep `main` always deployable.** It is what the rig pulls.

## Constraints that are easy to break by accident

**The repository is public.** `config/config.yaml` holds the Wi-Fi passphrase
and the router password and is gitignored. Never commit it, never echo secrets
into docs or commit messages, and keep `app.redact()` covering any new
secret-shaped config key.

**Port 80 is not negotiable.** Captive-portal detection only works there. The
service gets `CAP_NET_BIND_SERVICE` rather than running as root.

**The dashboard must survive a captive-portal webview.** No ES modules, no
external libraries, no CDNs, no WebSockets or SSE. Plain `fetch` polling, plain
scripts, everything served locally. `static/app.js` is written that way on
purpose.

**The measured link is `eth0`, not the Wi-Fi you are connected to.** `ping` is
pinned with `-I eth0`. Anything new that measures the uplink must pin it too, or
it will silently measure the wrong interface. **iperf3 is pinned differently
from ping**: `-B` takes an *address*, not an interface name, so `-B eth0` fails
with "Name or service not known" and the test never starts. `udpload.interface_address()`
resolves it, and falls back to the routing table when the modem is between
leases. Binding is worth having, but not at the cost of a test that refuses to
run.

**Don't enable iperf3 or `udp_load` by default.** The SIM is unlimited, so this
is not about the data bill. The *example* config ships them off because a fresh
clone has no server to talk to, and tests assert that. The rig's own
`config/config.yaml` has `udp_load` and `publish` switched on and pointed at
`viabotsurveys.com`.

**`udp_load` runs only during an active, unpaused run.** It is a deliberate
continuous load on the uplink: left running between walks it spends the link
for nothing and competes with the publisher sending the last run's clips, and
left running through a pause it contradicts what Pause means. The runner starts
and stops it alongside the camera, for the same reasons.

**`udp_load` bitrates are a compromise, and the uplink one has a hard ceiling
above it.** A live Formant teleop session on 2026-09-12 sent **645 kbit/s mean,
764 peak** robot-to-operator and **64 kbit/s mean, 104 peak** the other way:
one robot, one camera, 37 seconds. Against that, 5 Mbit/s up has been quoted for
teleop from memory, unsourced. `uplink_bitrate: 3M` splits them: ~4.6x the
measured session, and about two thirds of this link's own measured uplink
ceiling of **4.48 Mbit/s** (2026-09-17, good signal).

That ceiling is the constraint. Ping runs alongside the load and shares the
modem's buffer with it, so a rate at or above the link's capacity manufactures
dead zones the garage did not cause. The uplink test therefore runs in bursts
with silence between them, and the loaded seconds are thrown out of dead-zone
detection, but that bounds the damage rather than licensing any rate. **Testing
high is the safe direction only up to the point where the test becomes the
failure.** Raise
it only from a measurement of what a robot really sends with every camera an
operator would open; to ask "could this spot carry 5 Mbit/s" without disturbing
a survey, use `./scripts/capacity_test.sh --udp 5M`.

`downlink_bitrate: 5M` is not a model of anything. The real command stream is
64 kbit/s. It is a headroom check, affordable only because downlink measured
29.4 Mbit/s. Do not copy that reasoning to the uplink.

**Formant carries everything over WebRTC data channels, not media tracks.**
There is no `inbound-rtp` or `outbound-rtp` anywhere in a session dump, which
is why the bitrate cannot be read from the usual video stats and why Chrome's
task manager does not show it either. Read it from the **candidate-pair**
`[bytesReceived_in_bits/s]` / `[bytesSent_in_bits/s]` series in a
webrtc-internals dump.

**A flooded uplink makes iperf3 report what it sent, not what arrived.** The
end-of-test exchange cannot get back through a saturated uplink, so the client
falls back to its own counts: `end.sum` then reads as *exactly* the offered rate
at *exactly* 0.0% loss. `capacity_test.sh` printed 12 Mbit/s "delivered" over a
link the same run had just measured at 1.64 Mbit/s, and contradicted itself two
lines apart. Always parse the server's own per-second log
(`--get-server-output` + `parse_server_output`), the way `udpload` does; use
`end.sum` only when there is no server output, and say on the page that it is
the sender's number.

**The modem holds the buffer, not the router.** Measured 2026-09-18: offered 12
Mbit/s up, and the router's `4G-LTE` transmit counter showed 12,530 kbit/s
sustained. It handed every byte straight to the modem rather than queueing.
So the router's qdisc never backs up and there is nothing there to shrink or
manage; the bloat is inside the modem firmware, out of reach. The only lever on
uplink congestion is **sending less**. The router's counters are still worth
reading: `sed 's/:/ /' /proc/net/dev | awk '/4G-LTE/{print $10}'` sampled a
second apart is a real local uplink throughput measurement, needing nothing in
Dallas.

**Uplink is the half that matters most, and it cannot be measured at the rig.**
The robot *sends* video, so the heavy stream leaves the garage, and cellular
uplink is the weaker direction, so measuring only downlink flatters every
garage. iperf3 reports jitter and loss only at the receiving end, which for
uplink is the server: hence blocks, `--get-server-output`, and
`storage.backfill_samples` writing the readings onto the seconds they cover
afterwards. Do not "simplify" that into a live reading; there isn't one.

**`udp_load.datagram_bytes` must stay at 1200.** iperf3 defaults to 32 KB UDP
datagrams, which IP fragments into two dozen packets. Lose any one and the
whole datagram counts lost, so loss reads several times worse than a real
video packet would see, and every garage looks terrible.

**The drawtext escaping is not a typo.** `ESCAPED_COLON` in `workers/camera.py`
is two backslashes because a filtergraph is unescaped twice on the way in. One
backslash makes ffmpeg reject the whole graph, exit before writing a frame, and
the worker restart it forever, which cost a real survey walk. Single-quoting
the value instead fails differently ("Both text and text file provided"). If you
touch that string, run `test_overlay_colons_carry_two_backslashes` and, where
ffmpeg exists, `test_the_overlay_filtergraph_is_accepted_by_ffmpeg`.

**A broken overlay must never stop the recording.** `overlay_filter()` validates
the filtergraph once against a synthetic lavfi source and falls back to copy
mode if ffmpeg refuses it. Keep that fallback: losing the burned-in clock is an
inconvenience, losing every frame is a wasted trip to a garage.

**This rig loses power for real.** It runs off a battery through a
screw-terminal splice, and it has already died mid-operation more than once.
SQLite therefore runs `synchronous=FULL`, not NORMAL. Under NORMAL a commit is
acknowledged before it reaches the card, and a completed walk came back with its
samples intact but no result recorded. At one row a second the cost is not
measurable. For the same reason an interrupted run is analysed on the next
startup rather than merely closed: everything up to the cut is good data, and
discarding it means driving back to the garage.

**A walk shorter than one uplink cycle measures no uplink at all.** Uplink loss
is only countable at the far end, so the rig sends a fixed burst
(`uplink_block_s`, 10 s) and then asks the server what arrived. A burst cut
short by the run ending reports nothing, and the burst is followed by
`uplink_idle_s` (20 s) of silence, so the walk has to outlast the whole cycle.
A 23-second test walk therefore has a full downlink trace and no uplink
whatsoever, and the report used to drop the section rather than say so. It now
says why. The same applies to the tail of every real walk: up to one burst's
worth of uplink is lost at the end.

**The uplink load runs in bursts because a continuous one measures itself.**
The offered rate is above what a robot sends, and the modem holds roughly
1.75 Mbit (219 KB) of buffer, derived 2026-09-18 from a real walk's own median
RTT of 1381 ms against a 32 ms base and a 1.3 Mbit/s drain. Offer more than the
link can carry and that buffer fills in about a second, after which everything
sharing it queues behind the load test: ping included. That is what produced a
42.3% runnable reading at a spot with good signal. Fill time scales inversely
with the overshoot (3M into 1.3M fills in 1 s, 750k into 700k takes 35 s), so
the fix is not a gentler rate but a burst short enough that the queue cannot
build, and a gap long enough that it drains.

Seconds inside a burst, and `uplink_settle_s` after it, are written to
`samples.uplink_loaded` (2 sending, 1 settling) and **excluded from dead-zone
detection entirely** by `deadzones.measurable()`. They are dropped from the
percentage rather than counted as good, exactly as paused seconds are, and the
report prints both `walked_s` and `judged_s`. Three things follow that are easy
to break:

- The exclusion leaves a hole in the timeline, and a hole normally means a
  pause, which the detector refuses to stitch across. A burst is not a pause. The walk
  carried on, so each surviving sample carries `loaded_before_s` and
  `deadzones._gap()` subtracts it. Without that one bad ramp returns as three
  zones with three nearly identical clips.
- "No stream" for uplink must count only the seconds the rig was *sending*
  (`LOAD_SENDING`), or two thirds of every walk reads as the link having failed
  badly enough to take the test down with it. Same for the grey shading on the
  throughput plot.
- The live dead-zone counter on the phone applies the same exclusion, or it
  disagrees with the report.

**A link that could not carry what it was offered is a floor, not a
measurement.** `report.saturation()` counts the seconds delivering less than
`uplink_saturated_below` (0.85) of the offered rate and the report says so. This
matters because bufferbloat *saturates*: once the buffer is full the latency
stops rising, so a link that was slightly short and one that was hopelessly
short look identical in the trace. There is no honest way to recover what the
numbers would have been. The answer is to offer less and walk it again.

**A worker that finishes its work is not a worker that failed.** `Worker._loop`
backs off exponentially between restarts, which is right for something that
cannot start and wrong for the uplink load test, which returns after every
burst *by design*. Unreset, the gap doubled (2, 4, 8, 16, 32, 60)
until two thirds of a walk carried no uplink reading, indistinguishable from
coverage gaps. A `run_once()` lasting at least `healthy_run_s` now resets the
delay and is not announced as a restart. Keep that distinction if you add a
worker that works in blocks.

**iperf3 3.17 changed the credential encryption, and it is not compatible.**
Before 3.17 the client encrypts with PKCS#1 v1.5 padding; from 3.17 it uses
OAEP. A mismatch is rejected as **"test authorization failed"**, the same three
words a wrong password gets, and the real reason, `rsa routines::padding check
failed`, appears *only in the server's own log*. This cost a long hunt with
every credential provably correct: the rig runs 3.18 (Raspberry Pi OS trixie),
the Dallas server runs 3.16 (Ubuntu 24.04). `udp_load.auth_padding: auto` tries
the modern padding and falls back once on a rejection, and only when the local
iperf3 has `--use-pkcs1-padding` at all. When an authentication problem has a
correct password and a correct clock, compare `iperf3 --version` on both ends
before anything else.

**iperf3 hot-reloads the password but caches the key.** Measured against 3.16:
the `--authorized-users-path` file is re-read on every connection, so a rotated
password takes effect at once. But the `--rsa-private-key-path` is read once at
startup, so a rotated *key* does nothing until the server is restarted, and the
running server keeps accepting the old one. Rotating on the server therefore
means restarting `viabot-iperf3@5201` and `@5202` *and* copying the new
`iperf3_public.pem` to every rig. Miss either and you get "test authorization
failed" with a password that matches and a clock that is perfect.

**iperf3 authentication fails on a clock, not just on a password.** Every test
is signed with a timestamp, and a client more than **10 seconds** out is
rejected. Measured against iperf3 3.16: 10s authenticates, 11s does not, and
the message is "test authorization failed", exactly what a wrong password gets.
This Pi has no RTC, so an unsynchronised clock silently stops `udp_load`
working and points the blame at the credentials. `capacity_test.sh` reads the
server's clock off an HTTPS `Date:` header before testing, and `udpload.parse_error()`
spells out both causes.

**The radio plot puts dBm on the axis and quality in the colour.** Height is
RSRP as the modem reports it, a number that can be checked against the router
rather than taken on trust, and the line's colour *and thickness* are SINR. A
line that stays high and turns red is the case no single number catches: plenty
of signal, almost none of it usable. Quality is four named steps rather than a
smooth gradient, and **never travels as colour alone**: red against green is the
commonest colour-vision failure, so the key prints each band's dB range and the
line thickens as quality falls, which is what survives greyscale and
photocopying. Verified by rendering the page with `filter: grayscale(1)`. The
dBm window is fixed at -115..-65 rather than fitted to the data, so two garages
can be compared against each other.

**The radio score is the worse of two numbers, never the average.** RSRP says
how much of the cell's signal arrives; SINR says how much of what arrives is
signal rather than noise. They fail independently and need different remedies.
Weak-but-clean is a coverage hole an antenna can help, strong-but-dirty is
interference no antenna touches, so `radio.rate()` takes the minimum and names
the limiting half. An average lets a strong signal hide a filthy one, which is
the exact case a survey exists to find. RSSI and RSRQ are deliberately *not* in
the score: the four numbers carry two degrees of freedom, and those two restate
the relationship between the other two. The band cut points are the conventional
LTE ones and are **unvalidated for this robot**. See `docs/UNVERIFIED.md`.

**Timestamps are the product.** Video correlation depends entirely on the system
clock, and the Pi has no RTC. Preserve `clock_synced` reporting on samples, the
control page, and the start-of-run warning.

**The phone is a controller, not a viewer.** Erik asked for one screen with
Start / Pause / End, a small connection readout, and rig health, nothing else.
Results are read on a laptop afterwards. Resist adding reports, charts or video
to the phone; the screen is small and he is walking.

**The app cannot use `sudo`, and never could.** `viabot-survey.service` runs
with `CapabilityBoundingSet=CAP_NET_BIND_SERVICE`; a bounding set without
`CAP_SETUID`/`CAP_SETGID` makes sudo fail outright with *"unable to change to root
gid: Operation not permitted"*. The identical command from a login shell
succeeds, which is what hid this for so long: every manual test of "Apply
update" passed while the button did nothing. So the app asks for an update by
creating `<data_dir>/update-requested`, and `viabot-update.path` starts the
update. Anything else the app needs from systemd must go the same way; do not
add a sudo call to the app and test it over SSH.

**"The rig replied" does not mean the rig restarted.** `update.sh` fetches,
installs, and only then restarts the service, and the *old* process answers
`/api/health` perfectly happily throughout. The dashboard used to reload on the
first successful reply, about two seconds in, landing back on the old process
still showing the update as available. `/api/health` reports `started_at`, and
the page waits for it to *change*. Anything new that waits for the rig to come
back must do the same.

**The report is written before the full video exists, so the page asks.** The
recording is uploaded later, on request, and nothing re-renders the report when
it lands, because `render_html(full_video=...)` is never passed True by anything. So
the page carries the player hidden and the offer visible, and a same-origin
`HEAD video/full.mp4` on load swaps them. No script, or opened from a USB stick,
falls back to the offer. Do not give an element outside the tab bar the `tab`
class: the tab script selects `.tabs [data-tab]` now, but it used to select
`.tab`, and the request button wearing that class threw inside `show()` on every
report, killing everything later in the script while the page still looked
right.

**Camera failure must be impossible to miss.** A rig whose camera has died is
still cheerfully reporting connection quality, and the entire walk is wasted.
It gets a health chip *and* a full-width alert.

**"Apply update" updates the rig only.** `server/viabot_receiver.py` runs on a
different machine, from `/opt/viabot-receiver`. A change under `server/` needs
`git pull && sudo ./server/setup.sh --domain ...` on the server itself, and the
symptom of forgetting is that the merged change appears to do nothing at all.
Say so in any pull request that touches `server/`.

**A published report can never be changed.** `_append_chunk` treats a complete
file as final, and `enqueue_upload` leaves a `done` row done. Both are deliberate,
so a re-analysis cannot clobber an upload in flight. The consequence is that
improvements to the report page reach the next walk's report and never the ones
already uploaded.

**Uploading during a walk would poison the walk.** The publisher sends over
the same cellular link the survey is measuring, so it idles while a run is
active and resumes afterwards. `PublisherWorker.busy` is what enforces this;
anything new that talks to the network needs the same treatment.

**Publishing assumes the power will be cut.** A customer may switch the rig off
the moment a walk ends, or halfway through a 60 MB clip. The upload queue is in
SQLite, progress is recorded per chunk, and a resume always asks the receiver
how many bytes it holds rather than trusting the local number, because power can be
cut between a chunk landing and the rig learning that it did. Do not "optimise"
that HEAD away. `tests/test_publish.py` interrupts real transfers to a real
server; keep it that way, because nothing else catches this class of bug.

**Enabling `udp_load` moves the headline number.** Dead zones are detected from
ping, which shares the uplink with the load test. Since 2026-09-18 the loaded
seconds are excluded rather than judged, so the percentage is an estimate from
roughly two thirds of the walk rather than all of it. Still a fair sample at a
steady pace, but not the same measurement. Runs from before and after are not
comparable, and the thresholds were conceived for an idle link. See
`docs/UNVERIFIED.md`.

**Pause means "this time did not happen".** It stops measuring and recording
both, so paused seconds leave no samples and no video. That is what makes the
headline percentage meaningful, since it is a percentage of time and there is no
indoor positioning. Do not make Pause merely cosmetic.

## Layout

| Path | Role |
|---|---|
| `viabot_survey/runner.py` | Orchestrator: owns run state, samples at 1 Hz, classifies, analyses at run end |
| `viabot_survey/deadzones.py` | Detect dead zones from stored samples; cut a clip per zone |
| `viabot_survey/app.py` | Flask: captive portal, API, report building |
| `viabot_survey/workers/` | One file per measurement source, all subclass `base.Worker` |
| `viabot_survey/report.py` | Builds a run's result and renders it as the published page |
| `viabot_survey/chart.py` | Draws the throughput and radio plots as inline SVG, no library, because the report must open offline |
| `viabot_survey/radio.py` | RSRP + SINR into one 0-100 score, and which of the two is limiting it |
| `viabot_survey/workers/udpload.py` | Jitter and loss under teleop-sized UDP streams, both directions. The load case ping cannot see |
| `viabot_survey/publish.py` | Resumable upload client; the receiver's byte count is the authority |
| `viabot_survey/storage.py` | SQLite; add columns to `SAMPLE_COLUMNS` when extending `samples` |
| `server/viabot_receiver.py` | The cloud side: accepts uploads, serves reports. Deployed separately, not on the rig |
| `viabot_survey/router_client.py` | Modem stats. `AtOverSshRouterClient` is the one that works here: SSH to the router, AT to the modem. `normalize_signal` matches field names across firmwares for the HTTP clients |
| `scripts/setup.sh`, `setup_ap.sh` | Provisioning; both idempotent, both re-runnable |
| `scripts/thermal_test.sh` | Does the closed case cook the Pi? Decodes `get_throttled`, which is where the answer actually lives |
| `scripts/capacity_test.sh`, `viabot_survey/capacity.py` | The one-off ceiling test: how much can this link carry, as opposed to did it keep up. Saturates the uplink, so it refuses to run during a walk |
| `config/config.example.yaml` | The default layer *and* the documentation for every setting |

Configuration merges in three layers: the example file, then
`config/config.yaml`, then `VIABOT_SECTION_KEY` env vars. Adding a key to the
example file is what makes it exist. A user's older local config still boots.

## Testing

```bash
.venv/bin/python -m pytest        # 316 tests, no camera or rig needed
```

Most of the suite runs anywhere: workers are tested through their parsing and
command-building functions rather than by invoking `ping`/`ffmpeg`/`iperf3`.
Keep it that way for new tests.

The exceptions are marked `requires_ffmpeg` / `requires_iperf3` and skip when
the binary is absent. They
exist because the two bugs that actually reached the rig, a filtergraph ffmpeg
would not parse and clip cutting that had never run, were both invisible to
mocks. If you are changing the camera or clip code, install ffmpeg first
(`apt-get install -y --no-install-recommends ffmpeg`) so they run.

Run the app locally without hijacking your own browser:

```bash
.venv/bin/python -m viabot_survey --port 8080 --no-captive-portal
```

## Still open

**Read `docs/UNVERIFIED.md` first.** No Claude session has ever had SSH access to
the rig. Every fact comes from Erik pasting terminal output, so verify rather
than assume. `scripts/preflight.sh` answers most hardware questions in one pass.

The rig now runs: the access point comes up, the captive portal fires, a run
starts and ends, the camera records with a burned-in clock, and clips are cut.

Specifically open:

- Which SMA port is MAIN and which is DIV on the field router. Ports `a` and `c`
  are the two that produced working readings and the pair is in use, but the
  tests never separated them. Indoors the signal is strong enough that bare
  connectors couple plenty of RF, so every configuration looked alike. Do not
  write this down as established.
- The camera's real capabilities (`scripts/probe_camera.sh`).
- Whether a second server build goes cleanly. The first one is up and working
  (`viabotsurveys.com`, Vultr Dallas, 2026-09-14) but it took five fixes to
  `server/setup.sh` to get there, all of them merged. The next box is the test
  of whether the script is actually right.
- Whether the measured teleop bitrate holds across robots and camera
  settings. One session was measured; the config is set from it.
- What the *modem* does thermally. The Pi is settled: an hour closed and
  recording peaked at 57.4 C with no throttling at all (2026-09-13), leaving
  ~23 °C of headroom. But the modem has no sensor the Pi can read, and it
  works hardest exactly where signal is weak. Re-run `./scripts/thermal_test.sh`
  in a hot garage rather than assuming the bench figure transfers.
- Dead-zone thresholds are invented. `deadzone.provisional: true` (80% loss or
  1500 ms, sustained 5 s). The plan is to set them from one real survey walk.
  Do not quietly treat the current numbers as requirements. A finished run can
  be re-analysed with new ones via `POST /api/runs/<id>/analyse`.
- Whether a real garage produces sensible dead zones. The thresholds have never
  been checked against footage of a place anyone knows.

## Working style Erik has asked for

Ask clarifying questions **before** building, not after. A large, plausible
deliverable built on unverified assumptions is worse than a short question. When
something is genuinely ambiguous (thresholds, hardware capability, what the
robot actually needs), put the question to him rather than picking a default and
documenting the guess.
