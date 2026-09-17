# Troubleshooting

Start here:

```bash
sudo systemctl status viabot-survey      # running?
sudo journalctl -u viabot-survey -n 100  # what did it say?
curl -s http://127.0.0.1/api/health      # answering?
```

The dashboard's **Recent events** panel shows the same worker warnings without
needing SSH.

---

## "Apply update" does nothing

The rig stays on the old commit and the dashboard keeps offering the same
update. Check what the rig is actually on:

```bash
cd ~/claude-code-viabot && git log --oneline -1
```

If it has not moved, the update never ran. Before 2026-09-17 the app tried to
start the updater with `sudo`, which **cannot work** from inside
`viabot-survey.service`: its capability bounding set has no `CAP_SETUID`, so
sudo fails with *"unable to change to root gid"*. The same command run over SSH
succeeds, because a login shell is not restricted — so testing it by hand
proves nothing.

The fix is `viabot-update.path`, installed by `setup.sh`. A rig that has not
re-run setup since will not have it:

```bash
systemctl cat viabot-update.path >/dev/null 2>&1 && echo present || echo MISSING
```

If it is missing, update and re-provision over SSH, in this order:

```bash
cd ~/claude-code-viabot
./scripts/update.sh      # works by hand; it is only the button that was broken
./scripts/setup.sh       # installs and enables viabot-update.path
```

After that the dashboard button works on its own. To watch one go past:

```bash
journalctl -u viabot-update.service -f
```

## The Wi-Fi network doesn't appear

```bash
nmcli device status                      # wlan0 should be 'connected' to viabot-ap
nmcli connection show viabot-ap
iw dev wlan0 info | grep type            # should say 'type AP'
rfkill list                              # must not be soft or hard blocked
iw reg get                               # must not be country 00
```

Most often this is the regulatory domain: the radio stays blocked until a
country is set. Re-run `./scripts/setup_ap.sh`, which sets it, unblocks the
radio and rebuilds the profile.

If `wlan0` does not exist at all, the Wi-Fi interface is disabled in
`/boot/firmware/config.txt` (look for `dtoverlay=disable-wifi`).

## I'm connected but no page opens

The network works; only the automatic pop-up failed. Browse to
<http://192.168.50.1/> directly — that always works.

To fix the pop-up:

```bash
cat /etc/NetworkManager/dnsmasq-shared.d/viabot-captive.conf   # should contain address=/#/192.168.50.1
sudo systemctl reload NetworkManager
# From the phone's subnet, every name should resolve to the Pi:
nslookup example.com 192.168.50.1
```

Some phones cache a "this network has internet" verdict. Forget the network and
rejoin it.

Note that captive-portal webviews are cut-down browsers. If the page loads but
behaves strangely, open it in Safari or Chrome instead — there is no difference
in functionality.

## The banner says NO DATA

The ping worker is not getting replies. Check **Subsystems → ping** for the
error, then:

```bash
ip addr show eth0                        # has a 192.168.1.x address?
ip route show default                    # via eth0?
ping -I eth0 -c 3 8.8.8.8                # works by hand?
ping -c 3 192.168.1.1                    # is the router itself reachable?
```

If the router is reachable but 8.8.8.8 is not, the problem is upstream of the
Pi — check the router's own WAN status at <https://192.168.1.1/cgi-bin/luci/>.

`SO_BINDTODEVICE` or `Cannot assign requested address` in the error means `eth0`
had no address when ping started. The worker restarts itself; if it persists,
the Ethernet link is down.

## The camera isn't recording

**Subsystems → camera** shows the last few lines of ffmpeg's own output, which
usually names the problem outright.

```bash
ls -l /dev/video*                        # does the node exist?
groups viabot                            # must include 'video'
./scripts/probe_camera.sh                # what does it actually support?
```

`Inappropriate ioctl` or `Invalid argument` on startup means the configured
width/height/fps is not a mode this camera offers. The probe script prints a
block you can paste into `config/config.yaml`.

If the Pi is struggling — video stuttering, load average above 3 — switch
`camera.mode` to `copy`. That stores the camera's native MJPEG with no encoding
at all; you lose the burned-in clock but keep exact correlation through the
segment filenames.

If `groups` does not list `video`, log out and back in — group membership only
applies to new sessions.

## Timestamps look wrong

```bash
timedatectl status                       # 'System clock synchronized: yes'?
```

The Pi has no real-time clock. If it booted without a working uplink it may
never have set the time, and both the video labels and the sample timestamps
will be wrong. Get the cellular link up, wait a minute, confirm sync, then
restart the run. Runs that began unsynchronised are flagged in the events log
and on every sample (`clock_synced`).

## The rig didn't come back after an update

```bash
ssh viabot@garage-surveyor-01.local
sudo systemctl status viabot-survey
sudo journalctl -u viabot-update -n 50   # what the update itself did
cd ~/claude-code-viabot && git log --oneline -3
```

Roll back to the previous commit:

```bash
cd ~/claude-code-viabot
git log --oneline -5                     # find the last good commit
git reset --hard <that-commit>
sudo systemctl restart viabot-survey
```

Then say what happened in a Claude session so the underlying problem gets
fixed — and revert the bad pull request on GitHub, or the next update will
bring it straight back.

## "Check for updates" fails

It fetches from GitHub over the cellular link. `Could not resolve host` or a
timeout means the uplink is down or too weak. Update somewhere with decent
signal, before you go to the site.

## Disk filled up

```bash
df -h ~/claude-code-viabot/data
du -sh ~/claude-code-viabot/data/video/* | sort -h | tail
```

Video dominates. Collect what you need, then delete old run directories. The
camera worker refuses to start below `camera.min_free_disk_mb` (2 GB by
default) rather than filling the card and taking the database with it.

Deleting a run in the dashboard removes its rows from the database but **not**
its video — remove that directory yourself.

## Everything is confusing

Get back to a known state:

```bash
cd ~/claude-code-viabot
./scripts/uninstall.sh
./scripts/setup.sh
```

Your config and data survive both.
