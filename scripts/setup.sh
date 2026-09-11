#!/usr/bin/env bash
#
# One-shot provisioning for a fresh Raspberry Pi.
#
#   git clone https://github.com/erikbahl-afk/claude-code-viabot.git
#   cd claude-code-viabot
#   ./scripts/setup.sh
#
# Safe to re-run: every step checks its own state first. Re-run it after
# changing the `ap:` section of config/config.yaml.
#
# Flags:
#   --ssid NAME        Wi-Fi network name to broadcast
#   --password PSK     Wi-Fi passphrase (8-63 characters)
#   --timezone ZONE    e.g. America/Los_Angeles
#   --skip-apt         don't touch apt (useful when re-running offline)
#   --skip-ap          don't reconfigure Wi-Fi (app + service only)
#   --yes              never prompt

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROOT="$(repo_root)"
USER_NAME="$(service_user)"
SSID=""; PSK=""; TIMEZONE=""; SKIP_APT=0; SKIP_AP=0; ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssid)      SSID="$2"; shift 2 ;;
    --password)  PSK="$2"; shift 2 ;;
    --timezone)  TIMEZONE="$2"; shift 2 ;;
    --skip-apt)  SKIP_APT=1; shift ;;
    --skip-ap)   SKIP_AP=1; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
    *)           die "unknown option $1" ;;
  esac
done

[[ "$(id -u)" -eq 0 && -z "${SUDO_USER:-}" ]] && \
  warn "running as root with no SUDO_USER; the service will be installed for 'root'"

step "Preflight"
info "repository : $ROOT"
info "service user: $USER_NAME"
require_cmd python3 "Install it with: sudo apt install python3"
require_cmd git "Install it with: sudo apt install git"
command -v sudo >/dev/null || die "sudo is required"
[[ -d /run/systemd/system ]] || die "this rig expects systemd (Raspberry Pi OS)"
ok "preflight passed"

# ---------------------------------------------------------------------------
step "System packages"
PACKAGES=(
  python3-venv python3-pip git
  ffmpeg v4l-utils fonts-dejavu-core
  iperf3 iputils-ping
  network-manager dnsmasq-base nftables
  iw rfkill
)
if [[ "$SKIP_APT" -eq 1 ]]; then
  warn "skipping apt (--skip-apt)"
else
  MISSING=()
  for package in "${PACKAGES[@]}"; do
    dpkg -s "$package" >/dev/null 2>&1 || MISSING+=("$package")
  done
  if [[ ${#MISSING[@]} -eq 0 ]]; then
    ok "all packages already installed"
  else
    info "installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${MISSING[@]}"
    ok "packages installed"
  fi
fi

# ---------------------------------------------------------------------------
step "Python environment"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$ROOT/.venv"
  ok "created .venv"
fi
"$ROOT/.venv/bin/pip" install --quiet --upgrade pip
"$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"
ok "dependencies installed"

# ---------------------------------------------------------------------------
step "Configuration"
CONFIG="$ROOT/config/config.yaml"
if [[ ! -f "$CONFIG" ]]; then
  cp "$ROOT/config/config.example.yaml" "$CONFIG"
  ok "created config/config.yaml from the example"
else
  ok "config/config.yaml already exists (leaving it alone)"
fi
chmod 600 "$CONFIG"

CURRENT_SSID="$(yaml_get "$CONFIG" ap ssid)"
CURRENT_PSK="$(yaml_get "$CONFIG" ap password)"

if [[ -z "$SSID" ]]; then
  if [[ "$ASSUME_YES" -eq 1 || -n "${CURRENT_SSID:-}" ]]; then
    SSID="${CURRENT_SSID:-ViaBot-Survey}"
  else
    read -r -p "    Wi-Fi network name to broadcast [ViaBot-Survey]: " SSID
    SSID="${SSID:-ViaBot-Survey}"
  fi
fi

if [[ -z "$PSK" ]]; then
  if [[ "$CURRENT_PSK" == "changeme123" || -z "$CURRENT_PSK" ]]; then
    if [[ "$ASSUME_YES" -eq 1 ]]; then
      die "the Wi-Fi password is still the example default. Pass --password."
    fi
    warn "the Wi-Fi password is still the example default; set a real one now"
    while :; do
      read -r -s -p "    Wi-Fi password (8-63 chars): " PSK; echo
      [[ ${#PSK} -ge 8 && ${#PSK} -le 63 ]] && break
      warn "must be 8-63 characters"
    done
  else
    PSK="$CURRENT_PSK"
  fi
fi
[[ ${#PSK} -ge 8 && ${#PSK} -le 63 ]] || die "Wi-Fi password must be 8-63 characters"

# Write the values back with the YAML parser rather than sed, so quoting and
# special characters in the passphrase survive.
"$ROOT/.venv/bin/python" - "$CONFIG" "$SSID" "$PSK" <<'PY'
import sys, yaml
path, ssid, psk = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = yaml.safe_load(fh) or {}
data.setdefault("ap", {})["ssid"] = ssid
data["ap"]["password"] = psk
with open(path, "w") as fh:
    yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
PY
ok "config written (SSID '$SSID')"

AP_IFACE="$(yaml_get "$CONFIG" ap interface)";   AP_IFACE="${AP_IFACE:-wlan0}"
AP_ADDR="$(yaml_get "$CONFIG" ap address)";      AP_ADDR="${AP_ADDR:-192.168.50.1}"
AP_PREFIX="$(yaml_get "$CONFIG" ap prefix)";     AP_PREFIX="${AP_PREFIX:-24}"
AP_BAND="$(yaml_get "$CONFIG" ap band)";         AP_BAND="${AP_BAND:-bg}"
AP_CHANNEL="$(yaml_get "$CONFIG" ap channel)";   AP_CHANNEL="${AP_CHANNEL:-6}"
AP_COUNTRY="$(yaml_get "$CONFIG" ap country)";   AP_COUNTRY="${AP_COUNTRY:-US}"
BLOCK_NET="$(yaml_get "$CONFIG" ap block_client_internet)"; BLOCK_NET="${BLOCK_NET:-True}"
UPLINK="$(yaml_get "$CONFIG" uplink interface)"; UPLINK="${UPLINK:-eth0}"

# ---------------------------------------------------------------------------
step "Timezone"
# The timezone was never set during imaging. Video segment files are named in
# UTC (deliberately — that cannot drift), but the clock burned into the picture
# is local, and it is what you read when matching footage to where you walked.
CURRENT_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || echo '')"
info "currently: ${CURRENT_TZ:-unknown}"
if [[ -n "$TIMEZONE" ]]; then
  sudo timedatectl set-timezone "$TIMEZONE" && ok "set to $TIMEZONE"
elif [[ "$CURRENT_TZ" == "Etc/UTC" || "$CURRENT_TZ" == "UTC" || -z "$CURRENT_TZ" ]]; then
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    warn "timezone looks unset; leaving it at ${CURRENT_TZ:-unknown}."
    warn "the burned-in video clock will read UTC. Pass --timezone to change it."
  else
    warn "the timezone looks unset, so the clock burned into the video will read UTC"
    read -r -p "    Timezone (e.g. America/Los_Angeles, blank to keep): " ANSWER
    if [[ -n "$ANSWER" ]]; then
      sudo timedatectl set-timezone "$ANSWER" && ok "set to $ANSWER"
    else
      info "left as-is"
    fi
  fi
else
  ok "already set"
fi

if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
  ok "clock is NTP-synchronised"
else
  warn "clock is NOT NTP-synchronised yet. The Pi has no real-time clock, so"
  warn "every timestamp — and every video correlation — depends on this."
  warn "It should sort itself out once the cellular uplink is up."
fi

# ---------------------------------------------------------------------------
if [[ "$SKIP_AP" -eq 1 ]]; then
  step "Wi-Fi access point"
  warn "skipped (--skip-ap)"
else
  "$ROOT/scripts/setup_ap.sh"
fi

# ---------------------------------------------------------------------------
step "systemd services"
install_unit() {
  local name="$1" body="$2"
  local target="/etc/systemd/system/$name"
  if [[ -f "$target" ]] && printf '%s' "$body" | sudo cmp -s - "$target"; then
    ok "$name unchanged"
    return
  fi
  printf '%s' "$body" | sudo tee "$target" >/dev/null
  ok "installed $name"
}

install_unit viabot-survey.service "$(cat <<UNIT
[Unit]
Description=ViaBot garage coverage survey rig
Documentation=https://github.com/erikbahl-afk/claude-code-viabot
After=network.target NetworkManager.service
Wants=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$ROOT
Environment=PYTHONUNBUFFERED=1
ExecStart=$ROOT/.venv/bin/python -m viabot_survey
Restart=always
RestartSec=3
# Needed to listen on port 80 without running as root; the captive portal only
# works on port 80.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=no
# The camera node is owned by the 'video' group.
SupplementaryGroups=video
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
)"

install_unit viabot-update.service "$(cat <<UNIT
[Unit]
Description=Apply a ViaBot survey rig update
Documentation=https://github.com/erikbahl-afk/claude-code-viabot

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$ROOT
ExecStart=$ROOT/scripts/update.sh
TimeoutStartSec=600
StandardOutput=journal
StandardError=journal
UNIT
)"

# The app triggers its own update and restart, so it needs exactly these two
# commands and nothing more.
SUDOERS=$(cat <<SUDO
# Installed by ViaBot survey rig setup.sh — narrow, deliberate grants.
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl restart viabot-survey.service
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block viabot-update.service
$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl start viabot-update.service
SUDO
)
TMP_SUDOERS="$(mktemp)"
printf '%s\n' "$SUDOERS" > "$TMP_SUDOERS"
if sudo visudo -c -q -f "$TMP_SUDOERS"; then
  sudo install -m 0440 -o root -g root "$TMP_SUDOERS" /etc/sudoers.d/viabot-survey
  ok "installed /etc/sudoers.d/viabot-survey"
else
  rm -f "$TMP_SUDOERS"
  die "generated sudoers file failed validation; not installing it"
fi
rm -f "$TMP_SUDOERS"

# Give the service user access to the camera without a reboot-and-hope.
if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx video; then
  sudo usermod -aG video "$USER_NAME"
  ok "added $USER_NAME to the 'video' group"
fi

sudo systemctl daemon-reload
sudo systemctl enable --now viabot-survey.service
ok "viabot-survey.service enabled and started"

# ---------------------------------------------------------------------------
step "Verifying"
sleep 3
if systemctl is-active --quiet viabot-survey.service; then
  ok "service is running"
else
  warn "service is not running. Logs:"
  sudo journalctl -u viabot-survey.service -n 30 --no-pager || true
fi

if curl -fsS --max-time 5 "http://127.0.0.1/api/health" >/dev/null 2>&1; then
  ok "dashboard answered on port 80"
else
  warn "dashboard did not answer on port 80 yet — check 'journalctl -u viabot-survey -f'"
fi

cat <<SUMMARY

$(printf '%s' "$GREEN")Setup complete.$(printf '%s' "$OFF")

  1. On your phone, join Wi-Fi network:  $SSID
  2. The dashboard should open by itself. If it does not, browse to:
         http://$AP_ADDR/
  3. Measurements run through $UPLINK -> router -> cellular, not over the Wi-Fi
     you are connected to.

  Useful commands:
     sudo systemctl status viabot-survey     service state
     sudo journalctl -u viabot-survey -f     live logs
     ./scripts/probe_camera.sh               what the camera supports
     python3 scripts/probe_router.py         find the router's modem-stats API
     ./scripts/update.sh                     pull the latest code by hand

SUMMARY
