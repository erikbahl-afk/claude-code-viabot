# Getting a run off the rig

When a walk finishes, the rig writes a report page and queues it for upload,
along with a clip of every dead zone. The result is a link you can open on a
laptop, or send to a customer if the run is worth showing them.

## What the report looks like

**First tab, the answer.** The name you typed when starting the run, the
percentage of the walk that had a usable link, and a table of every dead zone:
when it started, how long it lasted, how bad it got, and a link to the footage.
Each clip runs from ten seconds before the dead zone, through all of it, to five
seconds after.

**Second tab, the evidence.** The whole walk video, latency and loss spread out
properly, what the link did under a teleop-sized load, and what the modem saw:
signal strength, quality, which cells it used and how often it changed between
them.

Both tabs print sensibly if it ever needs to be a PDF.

There is a **Rotate** button under each player, for footage from a camera
mounted on its side. It turns the picture 90 degrees at a time, remembers the
choice for the next report you open, and changes only how the video is shown
rather than the file. `camera.rotate` in the rig's config is the proper fix for
footage not yet recorded; this is for everything already uploaded, because a
published report can never be re-rendered.

Clicking a dead-zone clip opens it in a player on the page rather than a new
tab, which is what lets the Rotate button reach it. Without JavaScript, or
opened straight off a USB stick, the clips stay ordinary download links.

## What gets uploaded, and what does not

The clips go automatically. A typical walk is a few tens of megabytes.

The full recording does not. It is several hundred megabytes and it travels over
the cellular link, the same SIM the rig is there to measure. The second tab has
a Request the full video button instead. Press it and the rig sends the
recording the next time it is powered on and not walking.

Until then the recording is safe on the rig's card.

## If the power goes

This is designed for. A customer switching the rig off the second a walk ends
loses nothing:

* The queue is in the database, not in memory, so it survives a reboot.
* Progress is recorded chunk by chunk. Losing power costs at most half a
  megabyte of re-sending, not the whole file.
* On resume the rig asks the server how much it already received rather than
  trusting its own record, because power can be cut in the gap between a chunk
  arriving and the rig finding out it arrived.
* A half-uploaded clip is never shown as if it were complete.

Nothing needs doing. Power the rig back on and it finishes.

`tests/test_publish.py` interrupts real transfers to a real server to prove
this, and should stay that way. Nothing else catches this class of bug.

## Nothing uploads during a walk

Deliberately. Sending a clip over the link would create exactly the latency and
loss the survey exists to measure, and the report would be wrong in a way that
looked plausible. If a run starts while an upload is in progress, the upload
stops between chunks and picks up when the walk ends.

Anything new that talks to the network needs the same treatment.
`PublisherWorker.busy` is what enforces it.

## Turning it on

You need the receiving server first. See
[server/README.md](../server/README.md). It runs on any small cloud box, and
the same one answering the load tests is fine.

Then on the rig, in `config/config.yaml`:

```yaml
publish:
  enabled: true
  url: "https://surveys.example.com"
  token: "<the token from the server>"
```

`sudo systemctl restart viabot-survey`. The next finished walk publishes itself.

## Without a server

Everything above still happens except the upload. Every report is on the rig at

```
http://192.168.50.1/api/runs/<run-id>/report.html
```

and the run list is at `http://192.168.50.1/api/runs`. Clips are under
`data/clips/<run-id>/` on the card.

## Publishing an older run

Runs that finished before publishing was switched on were never queued. From a
laptop on the rig's Wi-Fi:

```bash
curl -X POST http://192.168.50.1/api/runs/<run-id>/publish
```

Safe to repeat. Anything already uploaded stays uploaded, and anything halfway
keeps its progress.

## A published report never changes

A complete file is treated as final and a finished upload stays finished. Both
are deliberate, so that re-analysing a run cannot clobber an upload in flight.

The consequence is worth knowing: an improvement to the report page reaches the
next walk's report and never the ones already uploaded. If you need an old run
re-published with a newer page, it has to be deleted on the server first.

## Who can read a report

Everything on the server sits behind a password. Each run also gets its own
share link, shown next to it on the server's index page, which opens that one
survey and nothing else. That is the link to send a customer.

Run identifiers contain the location name and the date, so they are easy to
guess. The password protects the reports, not the URL.
