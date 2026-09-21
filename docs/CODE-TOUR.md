# Code tour

Written for someone reviewing this code rather than extending it. It follows a
single measurement from the wire to the published report, says where each number
in that report comes from, and lists what is known to be wrong or unproven.

Read [ARCHITECTURE.md](ARCHITECTURE.md) first for the physical setup. The two
networks matter: the phone's Wi-Fi is the remote control, and `eth0` through the
router's modem is the link being measured. Confusing them explains most of the
early bugs in this repo.

## What the rig claims to produce

One walk produces one run, and a run produces:

* a percentage of time the link was usable,
* a list of dead zones with times and severity,
* a video clip of each dead zone,
* per-second samples of latency, loss, radio signal and load-test results.

Everything else in the codebase exists to produce those four things honestly.

## The path a measurement takes

**1. Workers collect.** Each measurement source is a class in
`viabot_survey/workers/`, all subclassing `base.Worker`. Each runs on its own
daemon thread and restarts itself on failure with a capped backoff. A worker
exposes `snapshot()` for the live dashboard and `sample_fields()` for the
recorded sample.

**2. The runner samples at 1 Hz.** `runner.py` holds a loop (`_sample_loop`)
scheduled against the monotonic clock. Once a second it calls `collect_sample()`,
which asks every enabled worker for its current reading, classifies the result
(`classify()`), and writes one row.

**3. Storage.** `storage.py` writes that row to SQLite. Two settings there are
deliberate and should not be relaxed: `journal_mode=WAL` so web requests can read
while a worker writes, and `synchronous=FULL` because this rig runs off a battery
through a screw-terminal splice and has already lost power mid-walk. Under
`NORMAL` a commit is acknowledged before it reaches the SD card, and one
completed walk came back with samples but no result.

Adding a column means adding it to `SAMPLE_COLUMNS` and to `ADDED_COLUMNS`, which
is what lets a rig that has been running for weeks pick up the new column without
losing its database.

**4. Analysis at the end.** `stop_run()` stops the camera and the load tests,
then calls `analyse_run()`, which:

* reads every sample for the run,
* filters out seconds the rig was loading the uplink itself
  (`deadzones.measurable()`, see below),
* groups the unusable ones into dead zones (`find_dead_zones()`),
* computes the headline numbers (`summarise()`),
* cuts a video clip per zone (`extract_clip()`),
* stores the summary.

Analysis works from stored samples, not live state, so a finished run can be
re-analysed with different thresholds by posting to
`/api/runs/<id>/analyse`. No re-walking required.

**5. Report.** `report.py` turns a run into a dict of numbers and then into one
self-contained HTML page. `chart.py` draws the plots as inline SVG with no
library, because the report has to open from a USB stick with no network.

**6. Publish.** `publish.py` and `workers/publisher.py` upload the report and
clips to `server/viabot_receiver.py`, which runs on a different machine. The
publisher idles while a run is active, because it would otherwise be sending
over the same cellular link the survey is measuring.

## Module map

| File | Responsibility |
|---|---|
| `runner.py` | Run state, the 1 Hz sample loop, classification, end-of-run analysis |
| `storage.py` | SQLite. Everything durable lives here |
| `app.py` | Flask: captive portal, dashboard, JSON API |
| `deadzones.py` | Which seconds count, which are dead zones, clip cutting |
| `report.py` | A run's result, and the published page |
| `chart.py` | Inline SVG plots |
| `radio.py` | RSRP and SINR into one 0-100 score |
| `publish.py` | Resumable chunked upload |
| `config.py` | Three-layer config merge |
| `workers/ping.py` | Latency and loss, pinned to `eth0` |
| `workers/udpload.py` | Jitter and loss under a teleop-sized UDP stream |
| `workers/camera.py` | ffmpeg recording with a burned-in clock |
| `workers/router.py` | Modem signal stats |
| `server/viabot_receiver.py` | The cloud side. Deployed separately, not on the rig |

## Where each number comes from

### The headline percentage

`deadzones.summarise()`. It is the share of *judged* seconds that were not inside
a dead zone. Judged seconds are all samples minus the ones excluded below.

It is a percentage of time, not of floor area. There is no indoor positioning, so
it only means anything if the operator walked at a steady pace and paused when
standing still. Pause writes no samples at all, which is what makes this work.

### Dead zones

`deadzones.is_unusable()` marks a sample as bad at 80% loss or 1500 ms latency.
Runs of bad samples become a zone if they last at least 5 seconds, and zones less
than 10 seconds apart are merged so one bad ramp does not report as six entries
with six near-identical clips.

Those thresholds are invented. See [UNVERIFIED.md](UNVERIFIED.md).

### Excluded seconds

This is the part most worth reviewing carefully.

The uplink load test offers more traffic than a robot sends, and the modem holds
roughly 1.75 Mbit of buffer (derived on 2026-09-18 from a real walk's median RTT
of 1381 ms against a 32 ms base and a 1.3 Mbit/s drain rate). If the offered rate
is above what the link can carry, that buffer fills in about a second, and ping
shares it. The result is latency the garage did not cause.

That happened. A walk at a spot with good signal came back 42.3% runnable.

So the uplink test now sends for `uplink_block_s` (10 s) and then stays quiet for
`uplink_idle_s` (20 s). Seconds inside a burst, plus `uplink_settle_s` (3 s)
after one while the buffer drains, are recorded in `samples.uplink_loaded` and
dropped from dead-zone detection entirely. They are dropped rather than counted
as good, the same way paused seconds are, and the report prints both `walked_s`
and `judged_s`.

Two details that follow from this and are easy to break:

* Filtering leaves a hole in the timeline, and a hole normally means a pause,
  which the detector refuses to stitch across. A burst is not a pause, so each
  surviving sample carries `loaded_before_s` and `deadzones._gap()` subtracts it.
* "No stream" for uplink counts only the seconds the rig was actually sending
  (`LOAD_SENDING`), or two thirds of every walk would read as total failure.

### Uplink versus downlink

They are measured differently and the asymmetry is not an accident. iperf3
reports jitter and loss only at the receiving end. For downlink the rig is the
receiver, so those numbers arrive live once a second. For uplink the receiver is
a server in Dallas, so the rig runs a fixed burst, asks for the server's own
per-second log (`--get-server-output`), and writes the readings back onto the
seconds they cover (`storage.backfill_samples`).

There is no live uplink reading. Do not simplify that away.

One consequence: a walk shorter than one full cycle (30 s) produces no uplink
data at all, and up to one burst is lost at the end of every walk.

### Saturation

`report.saturation()` counts seconds that delivered less than
`uplink_saturated_below` (0.85) of the offered rate, and the report says those
readings are a floor rather than a measurement. A full link loses packets because
it is full.

This matters because bufferbloat saturates. Once the buffer is full, latency
stops rising, so a link slightly short of the offered rate and one hopelessly
short look identical in the trace. There is no way to recover the number that
would have been there.

### The radio score

`radio.rate()` takes the *worse* of RSRP (how much signal arrives) and SINR (how
much of what arrives is usable), never the average. They fail independently and
need different fixes: weak but clean is a coverage hole an antenna can help,
strong but dirty is interference no antenna touches. An average would let a
strong signal hide a filthy one, which is the exact case a survey exists to find.

RSRQ and RSSI are deliberately left out of the score. The four numbers carry two
degrees of freedom and those two restate the relationship between the other two.

The band cut points are the conventional LTE ones and are unvalidated for this
robot.

## Things that are wrong right now

Known and unfixed as of 2026-09-21:

1. **Only the uplink load is excluded from dead-zone detection.** The downlink
   test runs continuously at 5 Mbit/s and its seconds are never excluded. The
   29.4 Mbit/s that justifies that rate was measured at a good spot. In a garage
   dead spot downlink capacity may be well under 5 Mbit/s, in which case the same
   bufferbloat problem applies in the other direction with nothing guarding
   against it.

2. **A hung load test is invisible.** The loop that reads iperf3's output has no
   timeout. On 2026-09-18, run `aew-test-03` logged nothing at all from `udp_up`
   for a nine-minute walk, and only 13 seconds of `uplink_loaded` were recorded.
   The worker appears healthy the whole time.

3. **The report goes quiet when the load test produced nothing.** If one
   direction is missing it says so with a reason. If both are missing the whole
   section disappears, which reads as "nothing to report".

4. **The samples CSV export is missing the load columns.** No `udp_up_*`,
   `udp_down_*` or `uplink_loaded`, so the CSV cannot show which seconds were
   excluded.

5. **A stuck iperf3 session on the server blocks the next walk.** Downlink uses
   `-t 0`, so there is no natural end for the server to wait for. If the control
   connection dies mid-walk the server keeps holding the session and the next run
   gets "the server is busy running a test".

6. **Load-test marking is imprecise.** The loaded window is predicted from the
   configured block length rather than observed from iperf3's own output. Real
   walks mark around 31% of seconds where 43% is expected.

## What has never been verified

[UNVERIFIED.md](UNVERIFIED.md) is the standing list and is kept honest. The short
version: the dead-zone thresholds are invented, the teleop bitrate comes from one
37-second capture of one robot with one camera, the radio band cut points are
generic LTE values, and no Claude session has ever had SSH access to the rig, so
every hardware fact in this repo came from someone pasting terminal output.

## Checking it yourself

```bash
.venv/bin/python -m pytest        # 316 tests, no hardware needed
```

Workers are tested through their parsing and command-building functions rather
than by invoking `ping`, `ffmpeg` or `iperf3`, so the suite runs anywhere. The
exceptions are marked `requires_ffmpeg` and `requires_iperf3` and skip when the
binary is absent. Those exist because the two bugs that actually reached the rig,
a filtergraph ffmpeg would not parse and clip cutting that had never run, were
both invisible to mocks.

Useful endpoints while connected to the rig's Wi-Fi at `192.168.50.1`:

| | |
|---|---|
| `/api/runs` | Every run with its summary |
| `/api/runs/<id>/report` | The full report as JSON, including load and saturation |
| `/api/runs/<id>/samples.csv` | Per-second samples |
| `/api/events?limit=500` | The event log. This is where a failing worker says why |
| `/api/config` | Merged config, with secrets masked |

To re-analyse a finished run with different thresholds, change them in
`config/config.yaml` and `POST /api/runs/<id>/analyse`.

## Conventions worth knowing before you change anything

* Port 80 is fixed. Captive-portal detection only works there, so the service
  gets `CAP_NET_BIND_SERVICE` instead of running as root.
* That same capability set means the app cannot use `sudo`. It asks for an update
  by touching a flag file that a systemd `.path` unit watches.
* The dashboard has to survive a captive-portal webview: no ES modules, no
  libraries, no CDNs, no WebSockets. Plain `fetch` polling.
* Anything that measures the uplink must pin to `eth0`. `ping` uses `-I eth0`;
  iperf3's `-B` takes an address, not an interface name, so `udpload.py` resolves
  it first.
* `config/config.example.yaml` is both the default layer and the documentation.
  Adding a key there is what makes it exist.

[CLAUDE.md](../CLAUDE.md) in the repository root holds the longer list, including
the specific mistakes that cost real survey walks.
