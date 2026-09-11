# What has never been checked on the real hardware

This rig was built and documented across two earlier sessions, neither of which
had SSH access to the Pi or admin access to the router — every command output in
those handoffs came from Erik running commands and pasting results back. The
current session has no route to the rig's LAN either.

So a number of things this software depends on are **expectations, not facts**.
`scripts/preflight.sh` answers all of them in one pass. Run it and paste the
output before trusting any of the below.

## Blocking — the design assumes these

| | Status | If the assumption is wrong |
|---|---|---|
| **`wlan0` can run as an access point** | Wi-Fi was *deliberately skipped* during imaging, so it is "unconfigured". Whether the radio enumerates, is rfkill-blocked, advertises AP mode, or is disabled by a `dtoverlay` has **never been checked**. Stock Pi 4B hardware supports AP mode, but that is general knowledge, not this unit. | The entire phone-based control plane needs rethinking — likely a USB Wi-Fi dongle, or falling back to controlling the rig over the wired LAN. |
| **NetworkManager manages the interfaces** | Never touched; the OS is stock as imaged. Raspberry Pi OS Bookworm onward defaults to NetworkManager, but this was not confirmed. | `scripts/setup_ap.sh` drives `nmcli` and would need rewriting against dhcpcd or `hostapd` directly. |
| **`viabot` has passwordless sudo** | Imager-created first users typically do, but `sudo -n true` was never run. | `setup.sh` prompts for a password instead — a nuisance, not a blocker. |

## Affects data quality

| | Status |
|---|---|
| **Timezone** | Never set during imaging; the Imager's locale screen was never opened. Whatever it defaulted to is unknown. Handled defensively — segment filenames are UTC and each run records its zone — and `setup.sh` offers to set it. |
| **Camera capabilities** | `--list-formats-ext` was never run. The one test capture defaulted to 352×288 because no size was specified; the real maximum is unknown. `scripts/probe_camera.sh` prints a config block from the hardware. |
| **`/dev/video1`** | Assumed to be a UVC metadata node paired with the `/dev/video0` capture node. General driver behaviour, not verified for this camera. |
| **Power headroom** | The battery's Ah/Wh rating was never read and no current draw or runtime figure exists. The Pi's undervoltage flags are now polled every second and stored per sample, which detects a sagging supply but does not predict runtime. |

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
  (the USB 3.0 assumption was wrong), and produced a valid test image.
- Both antennas are threaded onto the router, not merely resting.
- There is **no Y-splitter** in the power chain, and **no soldered joint**
  anywhere in it — see [HARDWARE.md](HARDWARE.md#the-splice).
