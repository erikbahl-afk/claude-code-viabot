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
pressing "Apply update" on the rig's dashboard — see `docs/WORKFLOW.md`.

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
it will silently measure the wrong interface.

**Don't enable iperf3 or `udp_load` by default.** The SIM is unlimited, so this
is no longer about the data bill — they ship disabled because neither has a
server to talk to yet and `udp_load` has no meaningful bitrate until Formant
supplies one. Tests assert both ship disabled.

**`udp_load` runs only during an active, unpaused run.** It is a deliberate
continuous load on the uplink: left running between walks it spends the link
for nothing and competes with the publisher sending the last run's clips, and
left running through a pause it contradicts what Pause means. The runner starts
and stops it alongside the camera, for the same reasons.

**`udp_load` bitrates are measured, not guessed — but from one session.** A
live Formant teleop session on 2026-09-12 sent **645 kbit/s mean, 764 peak**
robot-to-operator and **64 kbit/s mean, 104 peak** the other way. Hence
`uplink_bitrate: 1.5M` (deliberately about twice the measured rate — testing
high is the safe direction to be wrong in) and `downlink_bitrate: 300k`. That is what one robot's
camera settings produced over 37 seconds, not a Formant specification —
re-measure if the resolution or frame rate changes.

**Formant carries everything over WebRTC data channels, not media tracks.**
There is no `inbound-rtp` or `outbound-rtp` anywhere in a session dump, which
is why the bitrate cannot be read from the usual video stats and why Chrome's
task manager does not show it either. Read it from the **candidate-pair**
`[bytesReceived_in_bits/s]` / `[bytesSent_in_bits/s]` series in a
webrtc-internals dump.

**Uplink is the half that matters most, and it cannot be measured at the rig.**
The robot *sends* video, so the heavy stream leaves the garage — and cellular
uplink is the weaker direction, so measuring only downlink flatters every
garage. iperf3 reports jitter and loss only at the receiving end, which for
uplink is the server: hence blocks, `--get-server-output`, and
`storage.backfill_samples` writing the readings onto the seconds they cover
afterwards. Do not "simplify" that into a live reading; there isn't one.

**`udp_load.datagram_bytes` must stay at 1200.** iperf3 defaults to 32 KB UDP
datagrams, which IP fragments into two dozen packets — lose any one and the
whole datagram counts lost, so loss reads several times worse than a real
video packet would see, and every garage looks terrible.

**The drawtext escaping is not a typo.** `ESCAPED_COLON` in `workers/camera.py`
is two backslashes because a filtergraph is unescaped twice on the way in. One
backslash makes ffmpeg reject the whole graph, exit before writing a frame, and
the worker restart it forever — which cost a real survey walk. Single-quoting
the value instead fails differently ("Both text and text file provided"). If you
touch that string, run `test_overlay_colons_carry_two_backslashes` and, where
ffmpeg exists, `test_the_overlay_filtergraph_is_accepted_by_ffmpeg`.

**A broken overlay must never stop the recording.** `overlay_filter()` validates
the filtergraph once against a synthetic lavfi source and falls back to copy
mode if ffmpeg refuses it. Keep that fallback: losing the burned-in clock is an
inconvenience, losing every frame is a wasted trip to a garage.

**This rig loses power for real.** It runs off a battery through a
screw-terminal splice, and it has already died mid-operation more than once.
SQLite therefore runs `synchronous=FULL`, not NORMAL — under NORMAL a commit is
acknowledged before it reaches the card, and a completed walk came back with its
samples intact but no result recorded. At one row a second the cost is not
measurable. For the same reason an interrupted run is analysed on the next
startup rather than merely closed: everything up to the cut is good data, and
discarding it means driving back to the garage.

**Timestamps are the product.** Video correlation depends entirely on the system
clock, and the Pi has no RTC. Preserve `clock_synced` reporting on samples, the
control page, and the start-of-run warning.

**The phone is a controller, not a viewer.** Erik asked for one screen with
Start / Pause / End, a small connection readout, and rig health — nothing else.
Results are read on a laptop afterwards. Resist adding reports, charts or video
to the phone; the screen is small and he is walking.

**Camera failure must be impossible to miss.** A rig whose camera has died is
still cheerfully reporting connection quality, and the entire walk is wasted.
It gets a health chip *and* a full-width alert.

**Uploading during a walk would poison the walk.** The publisher sends over
the same cellular link the survey is measuring, so it idles while a run is
active and resumes afterwards. `PublisherWorker.busy` is what enforces this;
anything new that talks to the network needs the same treatment.

**Publishing assumes the power will be cut.** A customer may switch the rig off
the moment a walk ends, or halfway through a 60 MB clip. The upload queue is in
SQLite, progress is recorded per chunk, and a resume always asks the receiver
how many bytes it holds rather than trusting the local number — power can be
cut between a chunk landing and the rig learning that it did. Do not "optimise"
that HEAD away. `tests/test_publish.py` interrupts real transfers to a real
server; keep it that way, because nothing else catches this class of bug.

**Enabling `udp_load` moves the headline number.** Dead zones are detected
from ping, ping runs continuously, and with the load test active ping is
measuring a loaded link rather than an idle one. More dead zones will be found
in the same garage. Runs from before and after are not comparable, and the
thresholds were conceived for an idle link — see `docs/UNVERIFIED.md`.

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
| `viabot_survey/workers/udpload.py` | Jitter and loss under teleop-sized UDP streams, both directions — the load case ping cannot see |
| `viabot_survey/publish.py` | Resumable upload client; the receiver's byte count is the authority |
| `viabot_survey/storage.py` | SQLite; add columns to `SAMPLE_COLUMNS` when extending `samples` |
| `server/viabot_receiver.py` | The cloud side: accepts uploads, serves reports. Deployed separately, not on the rig |
| `viabot_survey/router_client.py` | Modem stats. `AtOverSshRouterClient` is the one that works here: SSH to the router, AT to the modem. `normalize_signal` matches field names across firmwares for the HTTP clients |
| `scripts/setup.sh`, `setup_ap.sh` | Provisioning; both idempotent, both re-runnable |
| `scripts/thermal_test.sh` | Does the closed case cook the Pi? Decodes `get_throttled`, which is where the answer actually lives |
| `config/config.example.yaml` | The default layer *and* the documentation for every setting |

Configuration merges in three layers: the example file, then
`config/config.yaml`, then `VIABOT_SECTION_KEY` env vars. Adding a key to the
example file is what makes it exist — a user's older local config still boots.

## Testing

```bash
.venv/bin/python -m pytest        # 211 tests, no camera or rig needed
```

Most of the suite runs anywhere: workers are tested through their parsing and
command-building functions rather than by invoking `ping`/`ffmpeg`/`iperf3`.
Keep it that way for new tests.

The exceptions are marked `requires_ffmpeg` / `requires_iperf3` and skip when
the binary is absent. They
exist because the two bugs that actually reached the rig — a filtergraph ffmpeg
would not parse, and clip cutting that had never run — were both invisible to
mocks. If you are changing the camera or clip code, install ffmpeg first
(`apt-get install -y --no-install-recommends ffmpeg`) so they run.

Run the app locally without hijacking your own browser:

```bash
.venv/bin/python -m viabot_survey --port 8080 --no-captive-portal
```

## Still open

**Read `docs/UNVERIFIED.md` first.** No Claude session has ever had SSH access to
the rig — every fact comes from Erik pasting terminal output, so verify rather
than assume. `scripts/preflight.sh` answers most hardware questions in one pass.

The rig now runs: the access point comes up, the captive portal fires, a run
starts and ends, the camera records with a burned-in clock, and clips are cut.

Specifically open:

- Which SMA port is MAIN and which is DIV on the field router. Ports `a` and `c`
  are the two that produced working readings and the pair is in use, but the
  tests never separated them — indoors the signal is strong enough that bare
  connectors couple plenty of RF, so every configuration looked alike. Do not
  write this down as established.
- The camera's real capabilities (`scripts/probe_camera.sh`).
- The Dallas server does not exist yet. `server/setup.sh` provisions the whole
  thing in one command on a fresh Vultr or Linode box (bundled transfer, not
  per-GB egress) — receiver, TLS via Caddy, and both authenticated iperf3
  servers. It needs a domain name pointed at the box first.
- Whether the measured teleop bitrate holds across robots and camera
  settings. One session was measured; the config is set from it.
- Whether the closed Apache 2800 case overheats. `./scripts/thermal_test.sh`
  answers it in an hour — run it with a survey **active**, since the camera
  encoding is most of the heat and an idle rig proves nothing.
- Dead-zone thresholds are invented — `deadzone.provisional: true` (80% loss or
  1500 ms, sustained 5 s). The plan is to set them from one real survey walk.
  Do not quietly treat the current numbers as requirements. A finished run can
  be re-analysed with new ones via `POST /api/runs/<id>/analyse`.
- Whether a real garage produces sensible dead zones. The thresholds have never
  been checked against footage of a place anyone knows.

## Working style Erik has asked for

Ask clarifying questions **before** building, not after. A large, plausible
deliverable built on unverified assumptions is worse than a short question. When
something is genuinely ambiguous — thresholds, hardware capability, what the
robot actually needs — put the question to him rather than picking a default and
documenting the guess.
