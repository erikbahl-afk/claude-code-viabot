# Troubleshooting

Start here:

```bash
sudo systemctl status viabot-survey      # running?
sudo journalctl -u viabot-survey -n 100  # what did it say?
curl -s http://127.0.0.1/api/health      # answering?
```

The dashboard's Recent events panel shows the same worker warnings without
needing SSH, and `/api/events?limit=500` gives you the lot as JSON.

## "Apply update" does nothing

The rig stays on the old commit and the dashboard keeps offering the same
update. Check what it is actually on:

```bash
cd ~/claude-code-viabot && git log --oneline -1
```

The app cannot use `sudo` and never could. `viabot-survey.service` runs with a
capability bounding set that has no `CAP_SETUID`, so sudo fails outright. The
same command over SSH succeeds, so testing it by hand proves nothing.

Instead the app touches a flag file and `viabot-update.path` picks it up. A rig
that has not re-run setup since 2026-09-17 will not have that unit:

```bash
systemctl cat viabot-update.path >/dev/null 2>&1 && echo present || echo MISSING
```

If missing, fix it over SSH in this order:

```bash
cd ~/claude-code-viabot
./scripts/update.sh      # works by hand, it was only the button that was broken
./scripts/setup.sh       # installs and enables viabot-update.path
```

To watch one go past: `journalctl -u viabot-update.service -f`.

## The Wi-Fi network does not appear

```bash
nmcli device status                      # wlan0 should be 'connected' to viabot-ap
nmcli connection show viabot-ap
iw dev wlan0 info | grep type            # should say 'type AP'
rfkill list                              # must not be soft or hard blocked
iw reg get                               # must not be country 00
```

Usually the regulatory domain. The radio stays blocked until a country is set.
Re-run `./scripts/setup_ap.sh`, which sets it, unblocks the radio and rebuilds
the profile.

If `wlan0` does not exist at all, Wi-Fi is disabled in
`/boot/firmware/config.txt`. Look for `dtoverlay=disable-wifi`.

## Connected, but no page opens

The network works and only the pop-up failed. Browse to
<http://192.168.50.1/> directly, which always works.

To fix the pop-up:

```bash
cat /etc/NetworkManager/dnsmasq-shared.d/viabot-captive.conf   # want address=/#/192.168.50.1
sudo systemctl reload NetworkManager
nslookup example.com 192.168.50.1        # every name should resolve to the Pi
```

Some phones cache a "this network has internet" verdict. Forget the network and
rejoin.

If the page loads but behaves strangely, open it in Safari or Chrome. Captive
portal webviews are cut-down browsers and there is no difference in
functionality.

## The banner says NO DATA

The ping worker is not getting replies. Check Subsystems then ping for the
error, then:

```bash
ip addr show eth0                        # has a 192.168.1.x address?
ip route show default                    # via eth0?
ping -I eth0 -c 3 8.8.8.8                # works by hand?
ping -c 3 192.168.1.1                    # is the router itself reachable?
```

If the router answers but 8.8.8.8 does not, the problem is upstream of the Pi.
Check the router's WAN status at <https://192.168.1.1/cgi-bin/luci/>.

`SO_BINDTODEVICE` or `Cannot assign requested address` means `eth0` had no
address when ping started. The worker restarts itself. If it persists, the
Ethernet link is down.

## The load test is not producing readings

The report says "Not measured on this run", or the uplink section is missing.
Check the event log first, because the reason is almost always written there.

**"test authorization failed"** has three causes and the message is identical
for all of them:

1. Wrong username or password in `udp_load`.
2. The rig's clock more than 10 seconds out. Every test is signed with a
   timestamp and this Pi has no RTC. Check `timedatectl status`.
3. An iperf3 version mismatch. 3.17 changed the credential encryption from
   PKCS#1 v1.5 to OAEP and the two do not interoperate. The rig runs 3.18, the
   Dallas server runs 3.16. `auth_padding: auto` handles this by falling back
   once, and you will see "retrying with the padding used before 3.17" in the
   events.

Compare `iperf3 --version` on both ends before assuming the password is wrong.
The real reason for a padding mismatch appears only in the server's own log, as
`rsa routines::padding check failed`.

**"the server is busy running a test"** means a previous session is still held
open on that port. When a rig's link dies mid-test the server never learns the
client has gone, and until it gives up every later test is refused.

Since 2026-09-21 both instances run with `--rcv-timeout 15000`, so a silent
session is dropped after 15 seconds instead of the 120 the default allows. If
you are still seeing this, check the running process rather than the file:

```bash
systemctl show -p ExecStart --value viabot-iperf3@5201 | grep -o -- '--rcv-timeout [0-9]*'
```

Nothing printed means the server is on an older unit and needs
`git pull && sudo ./server/setup.sh --domain ...` on the box itself. An update
applied to the rig does not touch the server.

To clear one by hand in the meantime:

```bash
sudo systemctl restart viabot-iperf3@5201 viabot-iperf3@5202
```

**Silence from `udp_up` with almost no `uplink_loaded` seconds** used to mean a
hung test that took the rest of the walk with it and logged nothing. A watchdog
now kills a test after 45 seconds of silence, so look for "the test stopped
responding and was restarted" in the event log. Seeing that repeatedly is a
link problem rather than a rig problem.

## The camera is not recording

Subsystems then camera shows the last few lines of ffmpeg's own output, which
usually names the problem outright.

```bash
ls -l /dev/video*                        # does the node exist?
groups viabot                            # must include 'video'
./scripts/probe_camera.sh                # what does it actually support?
```

`Inappropriate ioctl` or `Invalid argument` on startup means the configured
width, height or fps is not a mode this camera offers. The probe script prints a
block you can paste into `config/config.yaml`.

If the Pi is struggling, with stuttering video or load average above 3, switch
`camera.mode` to `copy`. That stores the camera's native MJPEG with no encoding.
You lose the burned-in clock but keep exact correlation through the segment
filenames.

If `groups` does not list `video`, log out and back in. Group membership only
applies to new sessions.

## Timestamps look wrong

```bash
timedatectl status                       # 'System clock synchronized: yes'?
```

The Pi has no real-time clock. If it booted without a working uplink it may
never have set the time, and both the video labels and the sample timestamps
will be wrong. Get the cellular link up, wait a minute, confirm sync, then
restart the run. Runs that began unsynchronised are flagged in the events log
and on every sample.

## The rig did not come back after an update

```bash
ssh viabot@garage-surveyor-01.local
sudo systemctl status viabot-survey
sudo journalctl -u viabot-update -n 50
cd ~/claude-code-viabot && git log --oneline -3
```

Roll back:

```bash
cd ~/claude-code-viabot
git log --oneline -5                     # find the last good commit
git reset --hard <that-commit>
sudo systemctl restart viabot-survey
```

Then revert the bad pull request on GitHub, or the next update brings it
straight back, and say what happened in a Claude session so the underlying
problem gets fixed.

## "Check for updates" fails

It fetches from GitHub over the cellular link. `Could not resolve host` or a
timeout means the uplink is down or too weak. Update somewhere with decent
signal before you go to site.

## Disk filled up

```bash
df -h ~/claude-code-viabot/data
du -sh ~/claude-code-viabot/data/video/* | sort -h | tail
```

Video dominates. Collect what you need, then delete old run directories. The
camera worker refuses to start below `camera.min_free_disk_mb` (2 GB by default)
rather than filling the card and taking the database with it.

Deleting a run in the dashboard removes its rows from the database but not its
video. Remove that directory yourself.

## Everything is confusing

Get back to a known state:

```bash
cd ~/claude-code-viabot
./scripts/uninstall.sh
./scripts/setup.sh
```

Config and data survive both.
