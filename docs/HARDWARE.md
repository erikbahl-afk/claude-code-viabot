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
early miscommunication about one — the bag of pigtails is spares.

**Out 1 → router:** straight into the router's 12 V barrel jack. Measured
12.1–12.4 V across two separate checks.

**Out 2 → Pi**, in order:

1. "Security" brand DC male barrel → screw-terminal pigtail, model **MA4G D5005M**
2. screw-terminal splice (not soldered) to a **NOCO GC019** 12-foot 12 V plug
   extension cable — factory male plug cut off, wires stripped, red → `+`,
   black → `−`
3. the NOCO cable's intact female socket, ~12 ft away
4. **Tuff Tech Hi-Speed Dual USB Charger**, 32 W (20 W USB-C PD + 12 W USB-A),
   box **SKU 24611** — *not* SKU 24610, which ships the wrong cable
5. USB-C cable → Raspberry Pi 4B USB-C power input

## Network chain

| Link | Detail |
|---|---|
| Router LAN 1 ↔ Pi `eth0` | Ethernet. The link under test. |
| Router LAN 2 | Free. Plug a laptop in here to collect data — the router's switch puts both ports on the same 192.168.1.x LAN. |
| Router WAN | Cellular only, via the internal 5G modem (`usb0` in the router's OS). No wired WAN exists or is needed. |
| Camera → Pi | USB. **USB 2.0 is fine** — the early assumption that it needed a blue USB 3.0 port was wrong. |
| Antennas | Two flat-panel "4G" antennas on the router's SMA connectors, confirmed threaded and snug. |

## Inventory

| Item | Detail |
|---|---|
| Raspberry Pi | 4B, Micro Connectors acrylic stackable case with fan (FAN4007) + heatsinks |
| microSD | Samsung 128 GB |
| Camera | USB, enumerates as `LRCP USB2.0`, USB ID `0bda:3035` (shows as Realtek in `lsusb`); `/dev/video0` plus a secondary `/dev/video1` stream |
| Router | ViaBot fleet spare, tagged "Reserved-router-.09" / "Remote-reboot works" / "Refurb Date: 8/13/26". Custom OpenWrt/LuCI, hostname `HL`, model `HL7621_S` (MediaTek MT7621, 256 MB RAM / 16 MB flash) |
| Battery | ViaBot fleet spare, "BMS ok! Nov 2025" / "New Battery 7/22" |
| DC-DC board | ViaBot in-house, two independent 12 V outputs |

## Software environment

| | |
|---|---|
| Pi OS | Raspberry Pi OS 64-bit, Debian trixie, kernel 6.18.34+rpt-rpi-v8, aarch64 |
| Pi hostname | **`garage-surveyor-01`** — note the "-or-". `garage-survey-01` was intended but this is what actually resolves. |
| SSH | `ssh viabot@garage-surveyor-01.local`, password auth. Works from anything on the router's LAN. Resolution is mDNS; no static IP is assigned. |
| Router admin | <https://192.168.1.1/cgi-bin/luci/>. The "Not secure" warning is expected — self-signed certificate on a local device. |
| Pi Wi-Fi | Was unconfigured. **This project turns `wlan0` into an access point** (see [ARCHITECTURE.md](ARCHITECTURE.md)). |
| Raspberry Pi Connect | Deliberately not enabled. |

Credentials for the router and the Pi login are not recorded in this repository,
which is public. Put the router password in `config/config.yaml` (gitignored)
only if you enable modem-statistics collection.

## Known gaps

- **Camera maximum resolution is uncharacterised.** The one test capture
  defaulted to 352×288 because no size was specified. Run
  `./scripts/probe_camera.sh` on the Pi; it prints a `camera:` block to paste
  into `config/config.yaml`.
- **Router Wi-Fi capability is unconfirmed** — irrelevant now, since the Pi
  provides the AP.
- **No rack or frame chosen.** Everything is loose. Before a real survey walk,
  consider at minimum that the 12 ft power run between the DC-DC board and the
  Pi's USB charger is a trip hazard and a disconnection risk, and that the
  camera needs to point consistently for the video to be worth correlating.
- **No GPIO button is needed any more.** The MARK button on the phone dashboard
  replaces it.

## Things that will bite you

**The clock.** The Pi has no real-time clock. It learns the time from NTP over
the cellular link after boot. If it boots without a working uplink, timestamps
— and therefore every video correlation — are wrong. The dashboard shows
`Clock synced`; check it before starting a run.

**Disk.** Recording 720p10 uses roughly 0.5–1 GB per hour. The 128 GB card is
plenty for a day, but the camera worker refuses to start below the
`min_free_disk_mb` floor (2 GB by default) rather than filling the card and
taking the database down with it.

**Power ordering.** The router needs to be up and holding a cellular
registration before the Pi's measurements mean anything. Give it a minute.
