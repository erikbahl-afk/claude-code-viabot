# First-time setup

Assumes the Pi boots, is on the network, and you can SSH to it, which is where
the hardware handoff left things. If not, see [HARDWARE.md](HARDWARE.md).

## 1. Get onto the Pi

Plug a laptop into the router's spare LAN port, then:

```bash
ssh viabot@garage-surveyor-01.local
```

Note the spelling: `garage-survey**or**-01`.

## 2. Clone, and check the hardware first

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/erikbahl-afk/claude-code-viabot.git
cd claude-code-viabot
./scripts/preflight.sh
```

Read-only, changes nothing. Paste the whole output into a Claude session before
going further. [UNVERIFIED.md](UNVERIFIED.md) explains why. The first section
matters most: if `wlan0` cannot run as an access point, the phone-based control
plane does not work as built, and that is much better to find out now.

## 3. Run setup

```bash
./scripts/setup.sh
```

It asks for a Wi-Fi passphrase for the network the rig will broadcast. Pick
something you can type on a phone. It is the only thing protecting the controls.

What it does, in order:

1. installs system packages (ffmpeg, iperf3, NetworkManager, nftables, v4l-utils, fonts)
2. creates `.venv` and installs the Python dependencies
3. creates `config/config.yaml` from the example, with your SSID and passphrase
4. sets the Wi-Fi regulatory domain and unblocks the radio
5. configures `wlan0` as an access point with captive-portal DNS
6. installs the nftables rule keeping AP clients off the cellular uplink
7. installs and starts `viabot-survey.service`
8. installs `viabot-update.path` and `viabot-update.service`, which is how the
   dashboard's update button works

Step 8 is worth understanding if you ever touch it. The app cannot call `sudo`,
because its capability bounding set has no `CAP_SETUID`. It asks for an update
by creating a flag file, and the `.path` unit watches for that file and starts
the update. See [ARCHITECTURE.md](ARCHITECTURE.md#updates).

Re-running setup is safe, and is the right move after editing the `ap:` section
of the config.

## 4. Characterise the camera

The handoff never established what the camera can do. The one test capture
defaulted to 352x288.

```bash
./scripts/probe_camera.sh
```

It lists every supported format and prints a ready-made `camera:` block. Paste
that into `config/config.yaml`, then:

```bash
sudo systemctl restart viabot-survey
```

## 5. Try it

Join the Wi-Fi network you just named. The dashboard should open by itself. If
it does not, browse to <http://192.168.50.1/>.

Check before trusting it:

* The banner shows GOOD with a plausible RTT, roughly 40 to 60 ms through the
  5G modem.
* Rig status, Clock synced says yes. If no, the Pi has not reached a time server
  yet. Wait a minute and reload. Everything in this rig depends on the clock.
* Rig status, Uplink eth0 is up with a 192.168.1.x address and the default route
  is via `eth0`.
* Subsystems, camera is enabled and not failed.

Then press START with a location name, walk around the building for two minutes,
press PAUSE and RESUME once to check they work, and press END. If it comes back
with a runnable percentage, the whole chain works.

To prove the part that matters, dead-zone detection and clip cutting, you need
somewhere with genuinely bad reception. A lift or a basement for ten seconds is
usually enough. Afterwards check that a clip landed in `data/clips/<run-id>/`.

## 6. Optional: modem signal statistics

Worth doing. RSRP, RSRQ and SINR are the most informative coverage data
available and cost no cellular data. The router's API is not characterised yet:

```bash
python3 scripts/probe_router.py --password '<router admin password>'
```

See [ROUTER.md](ROUTER.md).

## 7. Optional: the load test

Measures jitter and loss under a teleop-sized UDP stream, in both directions.
Needs your own iperf3 server and spends real cellular data. Read
[IPERF_SERVER.md](IPERF_SERVER.md) first.

## 8. Optional: publishing

Uploads finished reports and clips to a cloud server so they can be shared by
link instead of collected over the LAN. See [PUBLISHING.md](PUBLISHING.md).

## Moving to a second Pi

The repository is self-contained. Flash a card, boot it, then the same three
commands: `git clone`, `cd`, `./scripts/setup.sh`. Copy `config/config.yaml`
across by hand if you want identical settings. It is deliberately not in git,
because it holds passwords.

## Removing it

```bash
./scripts/uninstall.sh
```

Removes the services, the sudo grants, the AP profile, the captive-portal DNS
drop-in and the firewall rule. Leaves the repository, your config and your data
alone.
