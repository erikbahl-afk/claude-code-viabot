#!/usr/bin/env bash
#
# Answer, in one pass, every question about this Pi that has never actually
# been checked. Run it on the Pi and paste the whole output back.
#
#   ./scripts/preflight.sh
#
# Read-only: it inspects and changes nothing. Safe to run before setup.sh,
# after it, or at any point later.
#
# Nothing it prints is secret: no passwords, no keys, no SSIDs in use.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh" 2>/dev/null || {
  step() { printf '\n==> %s\n' "$*"; }
  info() { printf '    %s\n' "$*"; }
  warn() { printf '    ! %s\n' "$*"; }
  ok()   { printf '    ✓ %s\n' "$*"; }
  set -uo pipefail
}
set +e   # a missing tool is an answer, not a failure

show() { printf '    $ %s\n' "$*"; "$@" 2>&1 | sed 's/^/      /'; }

printf '===== ViaBot rig preflight — %s =====\n' "$(date -Is 2>/dev/null || date)"

# ---------------------------------------------------------------------------
step "1. Wi-Fi access point feasibility  [THE CRITICAL ONE]"
info "The rig's whole control plane assumes wlan0 can run as an AP."
if [[ -d /sys/class/net/wlan0 ]]; then
  ok "wlan0 exists"
  show ip -brief link show wlan0
else
  warn "NO wlan0 — the access-point design cannot work as built. Report this."
fi
show rfkill list
printf '    $ iw list | supported interface modes\n'
iw list 2>/dev/null | awk '/Supported interface modes/,/^\t[A-Z]/' | head -20 | sed 's/^/      /'
if iw list 2>/dev/null | awk '/Supported interface modes/,/Supported commands/' | grep -qw "AP"; then
  ok "driver advertises AP mode"
else
  warn "AP mode NOT advertised (or iw unavailable) — report this verbatim"
fi
printf '    $ grep -i wifi config.txt\n'
grep -i -e wifi -e disable-wifi /boot/firmware/config.txt /boot/config.txt 2>/dev/null | sed 's/^/      /' \
  || printf '      (no wifi-related overlay lines — good)\n'
show iw reg get

# ---------------------------------------------------------------------------
step "2. Network stack"
info "Provisioning drives NetworkManager specifically."
show systemctl is-active NetworkManager
show systemctl is-enabled dhcpcd
show nmcli device status
show ip -brief -4 addr
show ip route show default

# ---------------------------------------------------------------------------
step "3. Account, sudo, clock"
show id
printf '    $ sudo -n true  (passwordless sudo?)\n'
if sudo -n true 2>/dev/null; then
  printf '      yes — passwordless\n'
else
  printf '      no — sudo will prompt for a password (setup.sh handles this)\n'
fi
show timedatectl
info "Timezone was never set during imaging. Video segment names are UTC, but"
info "the clock burned into the picture is local — so this needs to be right."

# ---------------------------------------------------------------------------
step "4. Camera"
show ls -l /dev/video0 /dev/video1
show v4l2-ctl --list-devices
printf '    $ v4l2-ctl -d /dev/video0 --list-formats-ext\n'
v4l2-ctl -d /dev/video0 --list-formats-ext 2>&1 | sed 's/^/      /' \
  || printf '      (v4l2-ctl unavailable — sudo apt install v4l-utils)\n'
printf '    $ v4l2-ctl -d /dev/video1 --all | head -20   (capture node or metadata?)\n'
v4l2-ctl -d /dev/video1 --all 2>&1 | head -20 | sed 's/^/      /'
show lsusb

# ---------------------------------------------------------------------------
step "5. Power health"
info "The Pi's supply ends in a screw-terminal splice on a 12ft cable. A"
info "brownout there looks exactly like bad coverage in the survey data."
if command -v vcgencmd >/dev/null; then
  show vcgencmd get_throttled
  info "0x0 = clean. Bit 0 = undervoltage now; bit 16 = it happened since boot."
  show vcgencmd measure_volts
else
  warn "vcgencmd not found (not a Pi, or raspi-utils missing)"
fi

# ---------------------------------------------------------------------------
step "6. Resources"
show df -h /
show free -h
printf '    $ CPU temperature\n'
printf '      %s\n' "$(awk '{printf "%.1f C", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo unknown)"
show uptime

# ---------------------------------------------------------------------------
step "7. Uplink and router reachability"
show ping -c 3 -W 2 -I eth0 192.168.1.1
show ping -c 3 -W 2 -I eth0 8.8.8.8
printf '    $ next hop past the router (likely the modem)\n'
ping -c 2 -t 2 -W 2 -n 8.8.8.8 2>&1 | grep -i "exceeded" | head -3 | sed 's/^/      /' \
  || printf '      (no TTL-exceeded reply; ICMP may be filtered)\n'
printf '    $ public egress address\n'
printf '      %s\n' "$(curl -s --max-time 8 https://api.ipify.org 2>/dev/null || echo 'unavailable')"

# ---------------------------------------------------------------------------
step "8. Software packages"
for package in ffmpeg iperf3 v4l-utils network-manager dnsmasq-base nftables iw python3-venv fonts-dejavu-core; do
  if dpkg -s "$package" >/dev/null 2>&1; then
    printf '      %-22s installed\n' "$package"
  else
    printf '      %-22s MISSING (setup.sh installs it)\n' "$package"
  fi
done
show python3 --version

printf '\n===== end of preflight — paste everything above =====\n'
