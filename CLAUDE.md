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

**Don't enable iperf3 by default.** It is the only thing that spends cellular
data, and the SIM's plan is unknown. Tests assert it ships disabled.

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
| `viabot_survey/storage.py` | SQLite; add columns to `SAMPLE_COLUMNS` when extending `samples` |
| `viabot_survey/router_client.py` | Modem stats. `AtOverSshRouterClient` is the one that works here: SSH to the router, AT to the modem. `normalize_signal` matches field names across firmwares for the HTTP clients |
| `scripts/setup.sh`, `setup_ap.sh` | Provisioning; both idempotent, both re-runnable |
| `config/config.example.yaml` | The default layer *and* the documentation for every setting |

Configuration merges in three layers: the example file, then
`config/config.yaml`, then `VIABOT_SECTION_KEY` env vars. Adding a key to the
example file is what makes it exist — a user's older local config still boots.

## Testing

```bash
.venv/bin/python -m pytest        # 127 tests, no camera or rig needed
```

Most of the suite runs anywhere: workers are tested through their parsing and
command-building functions rather than by invoking `ping`/`ffmpeg`/`iperf3`.
Keep it that way for new tests.

The exceptions are marked `requires_ffmpeg` and skip when it is absent. They
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
- No iperf3 server exists yet; Erik plans to stand one up.
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
