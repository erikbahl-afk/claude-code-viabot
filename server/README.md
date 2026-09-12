# The survey receiver

A small service that accepts finished surveys from the rigs and serves them as
web pages. One Python file, one dependency, no database — a survey is a
directory of files.

It is separate from the rig on purpose. The rig is carried through garages and
loses power; this sits on a box that does not.

## What it does

- Accepts **resumable** uploads. A rig that loses power mid-transfer carries on
  from the exact byte it reached, rather than starting a 60 MB clip again over
  a cellular link.
- Serves each run's report at `/r/<run-id>/`, with the clips beside it.
- Keeps everything behind a password, with a per-run **share link** so one
  survey can be sent to a customer without handing over the rest.
- Records a request when somebody presses *Request the full video*, for the rig
  to pick up next time it is idle. Nothing can reach the rig directly — it sits
  behind carrier NAT — so it asks.

## Standing one up

Any small cloud box will do; the work is I/O, not CPU. Disk is what matters:
budget roughly **100 MB per walk** for clips, or **400 MB** if full videos are
often requested.

```bash
sudo adduser --system --group --home /opt/viabot-receiver viabot
sudo mkdir -p /opt/viabot-receiver /var/lib/viabot-receiver
sudo chown -R viabot:viabot /opt/viabot-receiver /var/lib/viabot-receiver

# Copy server/viabot_receiver.py and server/requirements.txt into
# /opt/viabot-receiver, then:
sudo -u viabot python3 -m venv /opt/viabot-receiver/.venv
sudo -u viabot /opt/viabot-receiver/.venv/bin/pip install -r /opt/viabot-receiver/requirements.txt
```

Generate the two secrets and write them where only root can read them:

```bash
printf 'VIABOT_RECEIVER_TOKEN=%s\nVIABOT_RECEIVER_VIEWER_PASSWORD=%s\n' \
  "$(openssl rand -base64 24)" "$(openssl rand -base64 18)" \
  | sudo tee /etc/viabot-receiver.env >/dev/null
sudo chmod 600 /etc/viabot-receiver.env
```

Then install the unit:

```bash
sudo cp server/viabot-receiver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now viabot-receiver
curl -s localhost:8089/healthz
```

### Put TLS in front of it

The service binds to `127.0.0.1` and speaks plain HTTP, because it is meant to
sit behind a reverse proxy that terminates TLS. Do not expose it directly: the
upload token and the viewer password would both cross the internet in clear.

With Caddy, the whole configuration is two lines:

```
surveys.example.com {
    reverse_proxy 127.0.0.1:8089
}
```

Caddy gets a certificate on its own. nginx with certbot works equally well —
set `client_max_body_size 0;` so it does not truncate a chunk.

## Pointing a rig at it

In `config/config.yaml` on the rig:

```yaml
publish:
  enabled: true
  url: "https://surveys.example.com"
  token: "<the VIABOT_RECEIVER_TOKEN from above>"
```

`sudo systemctl restart viabot-survey`, and the next finished walk publishes
itself. Runs finished *before* this was switched on can be queued with
`POST /api/runs/<run-id>/publish` on the rig.

## The upload protocol

Three requests, deliberately dull, so that a cut at any instant is recoverable.

| | |
|---|---|
| `HEAD /api/v1/runs/<run>/files/<name>` | How many bytes do you have? Returns `Upload-Offset`, and `Upload-Complete: 1` when finished. A file it has never seen is offset 0, not an error. |
| `PATCH /api/v1/runs/<run>/files/<name>` | Append a chunk. `Upload-Offset` must match exactly what the receiver holds, or it answers **409** with the real offset. |
| `POST /api/v1/runs/<run>/manifest` | What this run is called, so the index can list it before the report lands. |

Two properties make it survive a power cut at any moment:

**The receiver's file length *is* the offset.** Nothing is recorded alongside
the bytes, so nothing can disagree with them after a crash. Each chunk is
`fsync`ed before it is acknowledged, and a file is renamed from `.part` to its
real name only when complete — so a half-uploaded clip is never served as
though it were whole.

**A chunk is appended at a stated offset or not at all.** That turns a
duplicated or reordered chunk — the normal result of a retry over a flaky
cellular link — into a no-op rather than a corrupted file.

## Backups

Everything lives under `/var/lib/viabot-receiver/runs/`, one directory per run.
A file copy is a complete backup; there is no database to dump.
