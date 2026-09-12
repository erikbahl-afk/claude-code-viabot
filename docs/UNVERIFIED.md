# What has never been checked on the real hardware

This rig was built and documented across two earlier sessions, neither of which
had SSH access to the Pi or admin access to the router — every command output in
those handoffs came from Erik running commands and pasting results back. The
current session has no route to the rig's LAN either.

So a number of things this software depends on are **expectations, not facts**.
`scripts/preflight.sh` answers all of them in one pass. Run it and paste the
output before trusting any of the below.

## Proven end to end, 2026-09-11

A walk of the office produced, with nobody pressing anything during it:

| | |
|---|---|
| Runnable | 85.7% of 314 s walked |
| Dead zones | 1, lasting 45 s at 100% loss (an antenna unplugged deliberately) |
| Latency | 30 / 46 / 205 ms min / median / max |
| Clip | 8.5 MB, 60 s, opening at 14:17:06 — ten seconds before the outage |

The burned-in clock at the start of the clip matched the moment the link
dropped, which is the whole premise of the rig: a bad measurement resolves to
the right moment of footage without anyone marking it.

The result was recovered *after* the fact via `POST /api/runs/<id>/analyse`,
because the Pi lost power during the original wrap-up. That path is therefore
also proven.

## Established by the first real walk, 2026-09-11

The rig recorded a walk, detected a dead zone (an antenna unplugged for 45 s),
picked the right video and offset, and began cutting a clip. Then the Pi lost
power mid-cut, which exposed two things worth keeping in mind:

- **The Pi has no RTC, so log timestamps after a reboot are wrong until NTP
  catches up.** In this case the journal's first entry read 14:15:01 while
  `uptime -s` said 14:20:25 — five minutes apart, same boot. When reading logs
  around a power loss, trust `uptime -s` over the stamps.
- **The journal was not persistent**, so the log of the moments before the
  power loss was gone. `setup.sh` now creates `/var/log/journal`.

Both database durability and interrupted-run recovery were changed as a result;
see the commit for 2026-09-11.

## Still open

| | Status | If the assumption is wrong |
|---|---|---|
| **Power headroom** | The battery's Ah/Wh rating has never been read and no runtime figure exists. Undervoltage flags are polled every second and stored per sample — which detects a sagging supply but cannot predict how long the pack lasts. | Survey walks get cut short with no warning. |

## Answered by the first real setup, 2026-09-11

| | Finding |
|---|---|
| **`wlan0` serves an access point** | **Settled.** `setup_ap.sh` reported `wlan0 is in AP mode` at 192.168.50.1, the network was joined from a phone, and the control page loaded. The earlier `unavailable` state was NetworkManager's own Wi-Fi switch being off; the script now turns it on itself. |
| **The captive portal fires** | The phone showed "Sign in to ViaBot-Survey" and opened the page unprompted. |
| **The camera records** | **Settled, after a bug.** The first walk recorded nothing: ffmpeg rejected the overlay filtergraph and the worker restarted it in a loop. Fixed, and the filtergraph is now validated at startup against a synthetic source, so a future mistake here degrades to recording without the clock instead of recording nothing. |
| **Clips are cut** | Verified end to end against real footage, including the case where a dead zone straddles two segment files and they have to be joined. |
| **The rest of the chain** | A run started, paused, resumed, ended, and reported a percentage. Link, power, disk and clock all read healthy. |

## Answered by the preflight run of 2026-09-11

| | Finding |
|---|---|
| **NetworkManager manages the interfaces** | Confirmed. `NetworkManager` active, `dhcpcd` not installed, `eth0` connected at 192.168.1.139 with the default route via 192.168.1.1. |
| **`viabot` sudo** | In the `sudo` group, but **sudo prompts for a password** — it is not passwordless. `setup.sh` handles this; `update.sh` relies on the narrow NOPASSWD rules that `setup.sh` installs. |
| **`viabot` camera access** | Already in the `video` group, so no re-login is needed. |
| **Timezone** | Already set to `America/Los_Angeles` (PDT, −0700), clock NTP-synchronised. Better than assumed. |
| **Camera capabilities** | In MJPEG: 1920×1080, 1280×1024, 1280×720, 1024×768, 800×600, 640×480, 352×288, 320×240, 160×120 — every one at **30 fps and only 30 fps**. This is why `camera.capture_fps` defaults to null: asking a UVC device for an interval it does not advertise skews timestamps. |
| **`/dev/video1`** | Confirmed metadata-only (`Device Caps: 0x04a00000`). `/dev/video0` is the capture node. |
| **Disk and memory** | 117 GB card, 107 GB free. 1.8 GB RAM. CPU 34.6 °C at idle. |
| **Power** | `throttled=0x0` — clean, no undervoltage since boot. Measured on mains-adjacent conditions, not mid-walk. |
| **Uplink** | Router 0.4 ms; 8.8.8.8 at 40–57 ms, 0% loss. Egress address is in T-Mobile space. |
| **The modem** | A **Quectel EP06-A** — LTE Cat 6, *not* 5G, whatever LuCI's "Protocol: 5G" interface label says. The next hop past the router is 192.168.225.1, the Quectel factory default, consistent with it doing its own NAT. |
| **Signal metrics** | Working on the rig, verified 2026-09-12: `client: "at_ssh"` returned LTE band 12, cell 1452806, RSRP −100, RSRQ −12, SINR 11, RSSI −73 within seconds of a restart. The router has no modem API at all — `/ubus` 404s, there is no LuCI RPC, and `ubus list` carries no modem object. The readings come from `AT+QENG="servingcell"` on `/dev/ttyUSB2`, over SSH from the Pi. `AT+QRSRP` is unsupported on this firmware. See [ROUTER.md](ROUTER.md). |

## Never established at all

**What "good coverage" means for the robot.** No latency ceiling, throughput
floor, packet-loss tolerance, or specific robot failure mode has ever been
discussed — the prior sessions were entirely about physical assembly. The
thresholds that colour the dashboard are therefore invented. They are marked
`provisional: true` in the config and captioned as guesses in the UI. The plan
is to do one real survey walk and set them from what the data actually looks
like.

**Which SMA port on the field router is MAIN and which is DIV.** Ports were
labelled `a`, `b` and `c` and tested one at a time on 2026-09-12. `a` and `c`
each returned a working serving-cell reading (RSRP −104, SINR 11–12) and `b`
returned `ERROR`; a+c was also the fastest throughput pair. Antennas are fitted
to `a` and `c` on that basis, which is a reasonable call — but the test could
not discriminate. Indoors the ambient signal is strong (RSSI −77) and a bare
connector couples enough RF for the modem to camp regardless, so every
configuration looked alike and consecutive rounds contradicted each other.
Treat the MAIN/DIV assignment as unknown.

**Which garages are in scope**, how many levels, or what prompted the project.

## Things that are known

Verified in the original session and safe to rely on:

- 12.1–12.4 V at the router's input jack, across two separate checks.
- The Pi boots, is reachable over SSH at `garage-surveyor-01.local`, and its
  hostname is `garage-survey**or**-01`, not `garage-survey-01`.
- The router passes traffic; the Pi reaches `8.8.8.8` and resolves
  `google.com` at 0% loss, ~48–56 ms. The registration is LTE, not 5G —
  band 12 on T-Mobile (310/260), on a 5 MHz carrier.
- The camera enumerates as `LRCP USB2.0` on `/dev/video0`, works on **USB 2.0**
  (the USB 3.0 assumption was wrong), and produced a valid test image. Its full
  mode list is now known — see the table above.
- Both antennas are threaded onto the router, not merely resting.
- There is **no Y-splitter** in the power chain, and **no soldered joint**
  anywhere in it — see [HARDWARE.md](HARDWARE.md#the-splice).
