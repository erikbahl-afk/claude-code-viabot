# The load test

Measures jitter and packet loss under a UDP stream the size of a real
teleoperation session, in both directions. Shipped disabled, because a fresh
clone has no server to talk to.

Ping already finds dead zones. This finds the more interesting failure: a spot
that looks fine when idle and falls apart the moment you put video on it.
Carriers shape UDP differently from TCP and drop it first under contention, so
a link that passes a TCP speed test can still be useless for teleop.

For building the server, see [server/README.md](../server/README.md). That
covers the whole box, including the receiver that serves reports. This page is
the rig side.

## The two directions are measured differently

iperf3 reports jitter and loss only at the receiving end, and that shapes
everything.

**Downlink** carries the operator's commands. The rig is the receiver, so the
numbers arrive live, once a second.

**Uplink** carries the robot's video. The receiver is the server, so the rig
sends a fixed burst, asks for the server's own per-second log with
`--get-server-output`, and writes the readings back onto the seconds they cover.
There is no live uplink reading and there cannot be one.

Uplink is usually the half that decides whether a spot is workable. Cellular
uplink is the weaker direction and it carries the heavy stream, so measuring
only downlink flatters every garage.

## Configuration

```yaml
udp_load:
  enabled: true
  server: viabotsurveys.com
  username: viabot-rig
  password: "<from the server>"
  public_key_path: config/iperf3_public.pem

  uplink_enabled: true
  uplink_port: 5201
  uplink_bitrate: 3M
  uplink_block_s: 10
  uplink_idle_s: 20
  uplink_settle_s: 3

  downlink_enabled: true
  downlink_port: 5202
  downlink_bitrate: 5M

  datagram_bytes: 1200
  run_data_budget_mb: 2000
```

Each direction needs its own port, because one iperf3 server runs one test at a
time.

`config/config.example.yaml` documents every one of these inline, with the
reasoning. A few are worth repeating here.

**`datagram_bytes: 1200`.** Do not raise it. iperf3 defaults to 32 KB UDP
datagrams, which IP fragments into two dozen packets. Lose any one and the whole
datagram counts as lost, so loss reads several times worse than a real
1200-byte video packet would see, and every garage looks terrible.

**`uplink_bitrate: 3M`** is a compromise between two uncertain numbers. A live
Formant session on 2026-09-12 sent 645 kbit/s mean and 764 peak, one robot, one
camera, 37 seconds. Against that, 5 Mbit/s has been quoted from memory with no
source. 3M sits between them, and at roughly two thirds of this link's own
measured uplink ceiling of 4.48 Mbit/s.

That ceiling is the constraint, not the robot. Ping shares the modem's buffer
with the load test, so a rate at or above what the link can carry manufactures
dead zones the garage did not cause. To ask whether a spot could carry 5 Mbit/s
without disturbing a survey, use `./scripts/capacity_test.sh --udp 5M`.

**The burst schedule** exists for the same reason. The test sends for 10 seconds
then stays quiet for 20, and the loaded seconds are excluded from dead-zone
detection. [CODE-TOUR.md](CODE-TOUR.md#excluded-seconds) explains why in full.

**`downlink_bitrate: 5M`** is not a model of anything. The real command stream
is 64 kbit/s. It is a headroom check, affordable only because downlink measured
29.4 Mbit/s at a good spot. Do not copy that reasoning to the uplink.

## Data cost

The uplink test at 3 Mbit/s on a one-in-three duty cycle spends roughly 1
Mbit/s averaged, so about 225 MB for a 30-minute walk. Downlink at 5 Mbit/s runs
continuously, which is roughly 1.1 GB for the same walk and is server egress.

`run_data_budget_mb` stops launching tests once one run has moved that much. It
is a runaway guard rather than a cost control, so set it well above what a real
walk spends. Hitting it mid-walk leaves the rest of the garage unmeasured.

## Authentication, and the three ways it fails

The server requires credentials. An open iperf3 server is a bandwidth allowance
for anyone who finds the port.

All three failures below produce the same message, "test authorization failed":

1. **Wrong username or password.** The obvious one, and the least likely.
2. **A clock more than 10 seconds out.** Every test is signed with a timestamp.
   Measured against 3.16: 10 seconds authenticates, 11 does not. This Pi has no
   RTC, so an unsynchronised clock silently stops the load test working and
   points the blame at the credentials.
3. **A version mismatch.** iperf3 3.17 changed the credential encryption from
   PKCS#1 v1.5 padding to OAEP, and the two do not interoperate. The rig runs
   3.18 (Raspberry Pi OS trixie), the Dallas server runs 3.16 (Ubuntu 24.04).
   The real reason appears only in the server's log, as
   `rsa routines::padding check failed`.

`auth_padding: auto` handles the third by trying the modern padding and falling
back once on a rejection. When an authentication problem has a correct password
and a correct clock, compare `iperf3 --version` on both ends before anything
else.

One more, when rotating credentials: iperf3 re-reads the password file on every
connection, so a new password takes effect at once. It reads the private key
once at startup, so a new key does nothing until the server is restarted, and
the running server keeps accepting the old one.

## Checking it works

On the dashboard, open Subsystems. `udp_up` and `udp_down` should read
`running` with the server's address. The event log is where a failing test says
why:

```
curl -s 'http://192.168.50.1/api/events?limit=100'
```
