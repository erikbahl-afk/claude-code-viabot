# The physical rig

Recorded from the assembly handoff, plus what the software assumes. Everything
below was verified operational before the software work began.

## Power chain

```
Battery ──maroon connector + gland──► DC-DC board ──┬── Out 1 ──► Router 12V barrel
(fleet spare,                        (ViaBot, green │
 "BMS ok! Nov 2025",                  PCB, XT60 in) └── Out 2 ──► Pi power chain
 "New Battery 7/22")
```

The two outputs are independent. There is no Y-splitter in this rig, despite an
early miscommunication about one. The bag of pigtails is spares.

Out 1 goes straight into the router's 12 V barrel jack. Measured 12.1 to 12.4 V
across two separate checks.

Out 2 goes to the Pi, in order:

1. "Security" brand DC male barrel to screw-terminal pigtail, model **MA4G D5005M**
2. screw-terminal splice (not soldered) to a **NOCO GC019** 12-foot 12 V plug
   extension cable. Factory male plug cut off, wires stripped, red to `+`,
   black to `−`
3. the NOCO cable's intact female socket, about 12 ft away
4. **Tuff Tech Hi-Speed Dual USB Charger**, 32 W (20 W USB-C PD + 12 W USB-A),
   box **SKU 24611**. Not SKU 24610, which ships the wrong cable
5. USB-C cable to the Raspberry Pi 4B USB-C power input

## Network chain

| Link | Detail |
|---|---|
| Router LAN 1 to Pi `eth0` | Ethernet. The link under test. |
| Router LAN 2 | Free. Plug a laptop in here to collect data. The router's switch puts both ports on the same 192.168.1.x LAN. |
| Router WAN | Cellular only, via the internal 5G modem (`usb0` in the router's OS). No wired WAN exists or is needed. |
| Camera to Pi | USB. USB 2.0 is fine. The early assumption that it needed a blue USB 3.0 port was wrong. |
| Antennas | Two flat-panel "4G" antennas on the router's SMA connectors, confirmed threaded and snug. |

Which SMA port is MAIN and which is DIV is still not established. Ports `a` and
`c` are the two that produced working readings and that pair is in use, but the
tests never separated them. Indoors the signal is strong enough that bare
connectors couple plenty of RF, so every configuration looked alike.

## Inventory

| Item | Detail |
|---|---|
| Raspberry Pi | 4B, Micro Connectors acrylic stackable case with fan (FAN4007) and heatsinks |
| microSD | Samsung 128 GB |
| Camera | USB, enumerates as `LRCP USB2.0`, USB ID `0bda:3035` (shows as Realtek in `lsusb`). `/dev/video0` plus a secondary `/dev/video1` stream |
| Router | ViaBot fleet spare, tagged "Reserved-router-.09" / "Remote-reboot works" / "Refurb Date: 8/13/26". Custom OpenWrt/LuCI, hostname `HL`, model `HL7621_S` (MediaTek MT7621, 256 MB RAM, 16 MB flash) |
| Battery | ViaBot fleet spare, "BMS ok! Nov 2025" / "New Battery 7/22". Capacity unknown. No Ah or Wh rating was ever read and no runtime figure exists |
| DC-DC board | ViaBot in-house, two independent 12 V outputs. Has unpopulated footprints (DC002/DC003) beyond the two in use, relevant if this ever scales past one modem |

## Software environment

| | |
|---|---|
| Pi OS | Raspberry Pi OS 64-bit, Debian trixie, kernel 6.18.34+rpt-rpi-v8, aarch64 |
| Pi hostname | `garage-surveyor-01`. Note the "-or-". `garage-survey-01` was intended but this is what resolves |
| SSH | `ssh viabot@garage-surveyor-01.local`, password auth. Works from anything on the router's LAN. Resolution is mDNS, no static IP |
| Router admin | <https://192.168.1.1/cgi-bin/luci/>. The "Not secure" warning is expected, self-signed certificate on a local device |
| Pi Wi-Fi | Was unconfigured. This project turns `wlan0` into an access point, see [ARCHITECTURE.md](ARCHITECTURE.md) |
| Raspberry Pi Connect | Deliberately not enabled |

Credentials for the router and the Pi login are not in this repository, which is
public. Put the router password in `config/config.yaml` (gitignored) only if you
enable modem statistics.

## Thermal

An hour closed and recording peaked at **57.4 C with no throttling at all**
(2026-09-13), leaving about 23 C of headroom. That was on a bench.

The modem is the open question. It has no sensor the Pi can read, and it works
hardest exactly where signal is weak, which is the whole point of a garage
survey. Re-run `./scripts/thermal_test.sh` in a hot garage rather than assuming
the bench figure transfers.

## Known gaps

* **Camera maximum resolution is uncharacterised.** The one test capture
  defaulted to 352x288 because no size was specified. Run
  `./scripts/probe_camera.sh` on the Pi. It prints a `camera:` block to paste
  into `config/config.yaml`.
* **Router Wi-Fi capability is unconfirmed.** Irrelevant now, since the Pi
  provides the AP.
* **No rack or frame chosen.** Everything is loose. Before a real survey walk,
  consider that the 12 ft power run between the DC-DC board and the Pi's USB
  charger is a trip hazard and a disconnection risk, and that the camera needs
  to point consistently for the video to be worth correlating.
* **No GPIO button is needed.** The rig detects dead zones itself, so there is
  nothing to press while walking.

## The splice

There is no soldered joint anywhere in this power chain. The Pi's supply passes
through a plain screw-terminal splice, into the MA4G pigtail's own terminal
block, at the end of a twelve-foot cable that gets carried around a garage. A
Wago connector was considered and judged unnecessary. Neither Wago nor solder is
in the final assembly.

This is the most likely physical failure on the rig, and it fails in the worst
possible way: as an intermittent brownout, which in the data looks exactly like
a coverage problem. The Pi throttles, measurements go strange, and nothing in a
ping trace says "your power is loose".

The software watches for it. `vcgencmd get_throttled` is polled every second,
stored on every sample as `undervoltage`, shown on the dashboard, and logged as
an error the moment it trips. That turns a confusing survey into an obvious one,
but it is detection, not a fix. Secure that splice mechanically and
strain-relieve the cable before a real survey walk.

This rig has already lost power mid-operation more than once, which is why
SQLite runs `synchronous=FULL` and why an interrupted run is analysed on the
next startup rather than discarded.

## Things that will bite you

**The clock.** The Pi has no real-time clock. It learns the time from NTP over
the cellular link after boot. If it boots without a working uplink, timestamps
and therefore every video correlation are wrong. The dashboard shows Clock
synced. Check it before starting a run.

**The timezone was never set during imaging.** Video segment files are named in
UTC on purpose, so correlation cannot drift if the zone is changed later. The
clock burned into the picture is local time with its offset printed alongside,
because that is what you read when matching footage to where you walked. Each
run records the zone it was recorded in, and every video directory gets a
`manifest.json` saying which clock is which. `scripts/setup.sh` offers to set
the timezone if it is still unset.

**Disk.** Recording 720p10 uses roughly 0.5 to 1 GB per hour. The 128 GB card is
plenty for a day, but the camera worker refuses to start below
`min_free_disk_mb` (2 GB by default) rather than filling the card and taking the
database down with it.

**Power ordering.** The router needs to be up and holding a cellular
registration before the Pi's measurements mean anything. Give it a minute.
