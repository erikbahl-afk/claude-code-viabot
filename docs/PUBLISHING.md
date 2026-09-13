# Getting a run off the rig

When a walk finishes, the rig writes a report page and queues it for upload,
along with a clip of every dead zone. The result is a link you can open on a
laptop — and send to a customer, if the run is worth showing them.

## What the report looks like

**First tab — the answer.** The name you typed when starting the run, the
percentage of the walk that had a usable link, and a table of every dead zone:
when it started, how long it lasted, how bad it got, and a link to the footage.
Each clip runs from ten seconds before the dead zone, through all of it however
long it lasted, to five seconds after.

**Second tab — the evidence.** The whole walk video, latency and loss spread
out properly, and what the modem saw: signal strength, quality, which cells it
used and how often it changed between them.

Both tabs print sensibly, if it ever needs to be a PDF.

## What gets uploaded, and what does not

The clips go automatically. They are a few tens of megabytes for a typical
walk.

The full recording does **not**. It is several hundred megabytes and it travels
over the cellular link — the same SIM the rig is there to measure, on a plan
nobody has checked. The second tab has a **Request the full video** button
instead. Press it and the rig sends the recording the next time it is powered
on and not walking.

Until then the recording is safe on the rig's card, which has 107 GB free.

## If the power goes

This is designed for. A customer switching the rig off the second a walk ends
loses nothing:

- The queue is in the database, not in memory, so it is still there after a
  reboot.
- Progress is recorded chunk by chunk. Losing power costs at most half a
  megabyte of re-sending, not the whole file.
- On resume the rig asks the server how much it already received, rather than
  trusting its own record — because power can be cut in the gap between a chunk
  arriving and the rig finding out it arrived.
- A half-uploaded clip is never shown as if it were complete.

Nothing needs doing about it. Power the rig back on, and it finishes.

## Nothing uploads during a walk

Deliberately. Sending a clip over the link would create exactly the latency and
loss the survey exists to measure, and the report would be wrong in a way that
looked plausible. If a run starts while an upload is in progress, the upload
stops between chunks and picks up when the walk ends.

## Turning it on

You need the receiving server first — see [server/README.md](../server/README.md).
It runs on any small cloud box; the same one you were going to use for iperf3
is fine.

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

Safe to repeat: anything already uploaded stays uploaded, and anything halfway
keeps its progress.

## Who can read a report

Everything on the server sits behind a password. Each run also gets its own
**share link**, shown next to it on the server's index page, which opens that
one survey and nothing else — that is the link to send a customer.

Run identifiers contain the location name and the date, so they are easy to
guess. The password is what protects the reports, not the URL.
