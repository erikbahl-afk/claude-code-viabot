# The survey server

One small cloud box doing two jobs for the rigs:

1. **Receiving finished surveys** and serving them as web pages
   (`viabot_receiver.py`).
2. **Answering the UDP load tests** during a walk (`iperf3`, two instances).

They never compete: the load test runs *during* a survey and the upload runs
*after*, because the rig refuses to upload over a link it is measuring.

Both are separate from the rig on purpose. The rig is carried through garages
and loses power; this sits on a box that does not.

## Where to put it

**Dallas.** Not for latency — for comparability. The garages in scope are in
Florida, San Diego, the Bay Area, El Paso, North Carolina and Virginia, and a
test server near any one of them would flatter that city's results by skipping
hops a real session would cross. One consistent, well-connected, central server
makes every survey comparable to every other, and Dallas is a major US backbone
and peering hub.

**Vultr or Linode**, roughly $5–6/month, both with Dallas regions. The thing
that matters is that transfer is bundled rather than billed per gigabyte: the
downlink UDP test is server-to-rig egress, so every walk spends the server's
outbound allowance. A 30-minute walk at 1.5 Mbit/s is about 340 MB, so a 1 TB
allowance is a few thousand walks. Google Cloud's us-south1 is also physically
in Dallas but bills egress per gigabyte, which turns every test into a line
item.

Disk is the other consideration: budget roughly **100 MB per walk** for reports
and clips, or **400 MB** if full videos are often requested.

### What this measures, and what it cannot

The UDP test covers the **local leg only** — garage, cell tower, carrier
breakout — which is the part that actually varies from spot to spot inside one
garage, and therefore the part a coverage survey exists to find. It cannot see
the rest of the real path out to Formant's infrastructure and on to an operator
in California or India. That part is largely fixed and does not vary by
location, but it is not zero.

So treat these numbers as a **proxy**, and calibrate: run real Formant teleop
sessions with a real remote operator now and then, and check whether the local
numbers actually predicted how those felt.

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

## The iperf3 server

Install it and give it credentials. **Do not skip the authentication**: the port
has to be open to the whole internet, because the rig arrives from a different
carrier address on every walk, and an open iperf3 server is free bandwidth for
whoever finds it.

```bash
sudo apt-get install -y iperf3
sudo mkdir -p /etc/viabot

# Key pair. The rig gets the public half; the private half never leaves here.
sudo openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
  -out /etc/viabot/iperf3_private.pem -outform PEM
sudo openssl rsa -in /etc/viabot/iperf3_private.pem -outform PEM -pubout \
  -out /etc/viabot/iperf3_public.pem

# A user for the rig. Pick a long password; you will paste it into the rig's
# config once and never type it again.
USER=viabot-rig
read -rsp 'password: ' PASS; echo
printf '%s,%s\n' "$USER" \
  "$(printf '{%s}%s' "$USER" "$PASS" | sha256sum | awk '{print $1}')" \
  | sudo tee /etc/viabot/iperf3_users.csv >/dev/null

sudo chmod 600 /etc/viabot/iperf3_private.pem /etc/viabot/iperf3_users.csv
sudo chown -R viabot:viabot /etc/viabot

sudo cp server/viabot-iperf3@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now viabot-iperf3@5201    # uplink
sudo systemctl enable --now viabot-iperf3@5202    # downlink
```

**Two instances, on purpose.** One iperf3 server runs one test at a time — a
second client is told "the server is busy" — and the rig measures both
directions at once, because a teleop session is asymmetric and the two halves
fail differently.

Open **TCP and UDP ports 5201 and 5202** in the provider's firewall. iperf3
negotiates over TCP and then sends the test traffic over UDP, so it needs
both.

Copy `/etc/viabot/iperf3_public.pem` to the rig — it is a public key, so email
or a paste is fine — and put it somewhere like
`/home/viabot/claude-code-viabot/config/iperf3_public.pem`. Then on the rig:

```yaml
udp_load:
  enabled: true
  server: "surveys.example.com"
  username: "viabot-rig"
  password: "<the password from above>"
  public_key_path: "/home/viabot/claude-code-viabot/config/iperf3_public.pem"
  uplink_bitrate: "2M"      # what the robot sends: its video
  downlink_bitrate: "300k"  # what the operator sends: commands
```

Both bitrates come from one place — the operator's browser during a live
session, at `chrome://webrtc-internals`. Scroll past the event list to the
stats, and read `inbound-rtp (kind=video)` for what the robot sends up, and the
outbound streams for what the operator sends down. The values shipped in the
config are **placeholders**, not measurements.

Check it works before relying on it:

```bash
sudo systemctl restart viabot-survey
curl -s localhost/api/status | .venv/bin/python -c "
import json, sys
status = json.load(sys.stdin)['workers']
for name in ('udp_up', 'udp_down'):
    w = status[name]
    print(name, w['state'], 'loss', w['loss_pct'], 'jitter', w['jitter_ms'],
          'spent', w['run_mb'], 'MB', 'backfilled', w['backfilled_samples'])"
```

Uplink readings only appear once a block finishes (30 s by default) and are
then written onto the samples they cover, so `backfilled` climbing is what says
it is working. Downlink readings appear within a second or two.

> Both directions run **only during a walk**. Between runs they are stopped on
> purpose: they are a deliberate load on the uplink and would otherwise compete
> with the report upload.

> Leave `udp_load.enabled: false` until you have read the real bitrates off a
> live session. Testing at the wrong rate measures a link nobody will ever ask
> for.

## Backups

Everything lives under `/var/lib/viabot-receiver/runs/`, one directory per run.
A file copy is a complete backup; there is no database to dump.
