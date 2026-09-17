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

## Getting back onto the one that exists

The live server is `viabotsurveys.com` — a Vultr instance in Dallas, built
2026-09-14.

```bash
ssh root@viabotsurveys.com
```

`root`, not `viabot`: that is the provider's default account, and it is a
different login from the rig's. The password is in the Vultr control panel
under the instance overview, unless an SSH key was added.

Things worth knowing once you are in:

| What | Where |
|---|---|
| The three secrets | `/etc/viabot-receiver.env` (mode 600, root only) |
| iperf3 key pair and authorised users | `/etc/viabot/` |
| Uploaded surveys | `/var/lib/viabot-receiver/` |
| Service logs | `journalctl -u viabot-receiver -n 50` |
| iperf3 logs, per port | `journalctl -u viabot-iperf3@5201 -n 50` |

To check the rig's credentials against this box without printing any secret —
a public key fingerprint is public, and the hash is what this file already
stores:

```bash
openssl rsa -in /etc/viabot/iperf3_private.pem -pubout -outform DER \
  | sha256sum                       # must match the rig's iperf3_public.pem
cat /etc/viabot/iperf3_users.csv    # user,sha256 of "{user}password"
```

On the rig, the same two figures:

```bash
openssl rsa -pubin -in config/iperf3_public.pem -outform DER | sha256sum
.venv/bin/python -c "
import hashlib, yaml
c = yaml.safe_load(open('config/config.yaml'))['udp_load']
print(c['username'], hashlib.sha256(('{%s}%s' % (c['username'], c['password'])).encode()).hexdigest())"
```

A mismatched key is the likelier of the two after the server has been re-run
with `--rotate-secrets`, and it fails exactly like a wrong password:
`test authorization failed`, with nothing to say which.

## Standing one up

Any small cloud box will do; the work is I/O, not CPU. A **$5–6/month Vultr or
Linode instance in Dallas** is the intended shape — 1 GB of RAM is plenty.

**You need a domain name first.** Point an A record at the box's IP (a
subdomain of something ViaBot already owns is ideal — `surveys.viabot...`).
Without one there is no way to get a TLS certificate, and the upload token and
viewer password would cross the internet in clear text.

Then, on the box:

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/erikbahl-afk/claude-code-viabot.git
cd claude-code-viabot
sudo ./server/setup.sh --domain surveys.example.com
```

That is the whole thing. The script prints, once, the exact block to paste into
`config/config.yaml` on the rig — including the generated secrets — and tells
you which firewall ports to open.

It is safe to re-run after a repo update: it leaves existing secrets alone
unless you pass `--rotate-secrets`, and rotating means updating every rig.

If you genuinely have no domain, `--no-tls` will set it up on a bare IP and
warn you about what you are giving up. Prefer a domain.

### What the script does

1. Installs `python3-venv`, `iperf3`, `openssl` and Caddy.
2. Creates a `viabot` system account, `/opt/viabot-receiver` and
   `/var/lib/viabot-receiver`.
3. Generates three secrets into `/etc/viabot-receiver.env` (mode 600, root
   only): the upload token, the viewer password, and the iperf3 password.
4. Generates an RSA key pair for iperf3 authentication and builds the
   authorised-users file from the password.
5. Installs and starts three services — the receiver, and one iperf3 server per
   test direction.
6. Writes a Caddyfile that terminates TLS and proxies to the receiver on
   localhost, with no request body limit so a several-hundred-megabyte video
   chunk is not truncated.
7. Checks everything actually started, and prints the rig configuration.

### Firewall

Open these in the provider's control panel — the script does not touch it,
because on Vultr and Linode the firewall lives outside the box:

| Port | Why |
|---|---|
| TCP 80, 443 | The report pages. Caddy needs 80 to obtain its certificate. |
| TCP **and** UDP 5201 | Uplink load test |
| TCP **and** UDP 5202 | Downlink load test |

iperf3 negotiates over TCP and then sends the test traffic over UDP, so it
needs both protocols on both ports.

### Two iperf3 instances, on purpose

One iperf3 server runs one test at a time — a second client is told "the server
is busy" — and the rig measures both directions at once, because a teleop
session is asymmetric and the two halves fail differently.

## Pointing a rig at it

`setup.sh` prints the exact block to paste into `config/config.yaml` on the
rig, with the real secrets filled in. It looks like this:

```yaml
publish:
  enabled: true
  url: "https://surveys.example.com"
  token: "<printed by setup.sh>"

udp_load:
  enabled: true
  server: "surveys.example.com"
  username: "viabot-rig"
  password: "<printed by setup.sh>"
  public_key_path: "/home/viabot/claude-code-viabot/config/iperf3_public.pem"
```

Then `sudo systemctl restart viabot-survey` on the rig, and the next finished
walk publishes itself. Runs that finished *before* publishing was switched on
were never queued; queue one with
`curl -X POST http://192.168.50.1/api/runs/<run-id>/publish`.

> `config/config.yaml` is gitignored, and every value under a key named
> `password` or `token` is masked before it reaches the database, the API or
> the dashboard. Still — this repository is public. Do not paste these into an
> issue, a commit, or a Claude session.

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

`setup.sh` handles all of this; what follows is what it did, for when something
needs checking by hand.

Two systemd instances of `iperf3 --server`, on ports 5201 and 5202, both
started from `viabot-iperf3@.service`. Both authenticate against
`/etc/viabot/iperf3_users.csv` using the RSA key pair in the same directory.

**Authentication is not optional here.** The ports have to be open to the whole
internet, because the rig arrives from a different carrier address on every
walk, and an open iperf3 server is a bandwidth allowance that anyone who finds
it can spend.

Copy `/etc/viabot/iperf3_public.pem` to the rig — it is a public key, so email
or a paste is fine.

Check it from the rig after configuring:

```bash
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

### What to set the bitrates to

They are already set from a real measurement — `uplink_bitrate: 1.5M`,
`downlink_bitrate: 300k`. See `docs/UNVERIFIED.md` for where those came from
and when to re-measure.

## Backups

Everything lives under `/var/lib/viabot-receiver/runs/`, one directory per run.
A file copy is a complete backup; there is no database to dump.
