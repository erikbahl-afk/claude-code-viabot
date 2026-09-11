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

**Timestamps are the product.** Video correlation depends entirely on the system
clock, and the Pi has no RTC. Preserve `clock_synced` reporting on samples, the
dashboard, and the start-of-run warning.

## Layout

| Path | Role |
|---|---|
| `viabot_survey/runner.py` | Orchestrator: owns run state, samples at 1 Hz, classifies |
| `viabot_survey/app.py` | Flask: captive portal, API, report building |
| `viabot_survey/workers/` | One file per measurement source, all subclass `base.Worker` |
| `viabot_survey/storage.py` | SQLite; add columns to `SAMPLE_COLUMNS` when extending `samples` |
| `viabot_survey/router_client.py` | Modem stats; `normalize_signal` matches field names across firmwares |
| `scripts/setup.sh`, `setup_ap.sh` | Provisioning; both idempotent, both re-runnable |
| `config/config.example.yaml` | The default layer *and* the documentation for every setting |

Configuration merges in three layers: the example file, then
`config/config.yaml`, then `VIABOT_SECTION_KEY` env vars. Adding a key to the
example file is what makes it exist — a user's older local config still boots.

## Testing

```bash
.venv/bin/python -m pytest        # 84 tests, no hardware needed
```

The suite runs anywhere: workers are tested through their parsing and
command-building functions rather than by invoking `ping`/`ffmpeg`/`iperf3`.
Keep it that way — the container Claude runs in has none of those binaries.

Run the app locally without hijacking your own browser:

```bash
.venv/bin/python -m viabot_survey --port 8080 --no-captive-portal
```

## Still open

- The router's modem-stats API is uncharacterised. `scripts/probe_router.py`
  exists to discover it; `router.client` defaults to `"null"`.
- The camera's maximum resolution is uncharacterised
  (`scripts/probe_camera.sh`).
- No iperf3 server exists yet; Erik plans to stand one up.
- Nothing has been tested on the physical rig yet — everything in this
  repository is verified by unit tests and local smoke runs only. Treat the
  first on-device run as the real test, especially `setup_ap.sh`, which touches
  NetworkManager, dnsmasq and nftables and cannot be exercised in CI.
