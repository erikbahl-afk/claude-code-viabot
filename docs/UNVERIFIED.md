# What has never been checked on the real hardware

This rig was built and documented across two earlier sessions, neither of which
had SSH access to the Pi or admin access to the router — every command output in
those handoffs came from Erik running commands and pasting results back. The
current session has no route to the rig's LAN either.

So a number of things this software depends on are **expectations, not facts**.
`scripts/preflight.sh` answers all of them in one pass. Run it and paste the
output before trusting any of the below.

## Still open

| | Status | If the assumption is wrong |
|---|---|---|
| **`wlan0` actually serves an access point** | **Nearly settled.** The radio is present, unblocked, advertises AP mode, and the `unavailable` state turned out to be NetworkManager's own Wi-Fi switch being off — `nmcli radio wifi on` moved it to `disconnected`, the healthy idle state. `setup_ap.sh` now does that itself. What has still never been done is bringing up an actual AP and joining it from a phone. | The control plane needs rethinking — likely a USB Wi-Fi dongle, or controlling the rig over the wired LAN. |
| **Power headroom** | The battery's Ah/Wh rating has never been read and no runtime figure exists. Undervoltage flags are polled every second and stored per sample — which detects a sagging supply but cannot predict how long the pack lasts. | Survey walks get cut short with no warning. |

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
| **The modem** | The next hop past the router is **192.168.225.1** — the Quectel factory default, consistent with the modem doing its own NAT. See [ROUTER.md](ROUTER.md). |

## Never established at all

**What "good coverage" means for the robot.** No latency ceiling, throughput
floor, packet-loss tolerance, or specific robot failure mode has ever been
discussed — the prior sessions were entirely about physical assembly. The
thresholds that colour the dashboard are therefore invented. They are marked
`provisional: true` in the config and captioned as guesses in the UI. The plan
is to do one real survey walk and set them from what the data actually looks
like.

**Where the signal metrics live.** See [ROUTER.md](ROUTER.md) — the modem
appears to do its own NAT and present as a plain Ethernet adapter, which would
mean the router has nothing to query.

**Which garages are in scope**, how many levels, or what prompted the project.

## Things that are known

Verified in the original session and safe to rely on:

- 12.1–12.4 V at the router's input jack, across two separate checks.
- The Pi boots, is reachable over SSH at `garage-surveyor-01.local`, and its
  hostname is `garage-survey**or**-01`, not `garage-survey-01`.
- The router holds a 5G registration and passes traffic; the Pi reaches
  `8.8.8.8` and resolves `google.com` at 0% loss, ~48–56 ms.
- The camera enumerates as `LRCP USB2.0` on `/dev/video0`, works on **USB 2.0**
  (the USB 3.0 assumption was wrong), and produced a valid test image. Its full
  mode list is now known — see the table above.
- Both antennas are threaded onto the router, not merely resting.
- There is **no Y-splitter** in the power chain, and **no soldered joint**
  anywhere in it — see [HARDWARE.md](HARDWARE.md#the-splice).
