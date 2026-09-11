#!/usr/bin/env bash
#
# Turn the Pi's built-in Wi-Fi into an access point serving the survey
# dashboard, with captive-portal detection so phones open it automatically.
#
#   ./scripts/setup_ap.sh
#
# Reads the `ap:` section of config/config.yaml. Re-run after editing it.
#
# How this fits together:
#   * NetworkManager runs the AP in `shared` mode, which also starts a dnsmasq
#     bound to wlan0 for DHCP and DNS.
#   * A drop-in in /etc/NetworkManager/dnsmasq-shared.d points every DNS name
#     at the Pi, so a phone's connectivity probe lands on our web server and
#     gets a redirect instead of the "you have internet" response it expects.
#     That is what makes the dashboard pop up on its own.
#   * An nftables rule stops AP clients from routing out through the modem, so
#     phone background traffic never competes with the measurements.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROOT="$(repo_root)"
CONFIG="$ROOT/config/config.yaml"
[[ -f "$CONFIG" ]] || die "config/config.yaml not found — run ./scripts/setup.sh first"

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

read_cfg() { "$PY" - "$CONFIG" "$1" "$2" <<'PY'
import sys, yaml
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh) or {}
value = (data.get(sys.argv[2]) or {}).get(sys.argv[3])
print("" if value is None else value)
PY
}

SSID="$(read_cfg ap ssid)"
PSK="$(read_cfg ap password)"
IFACE="$(read_cfg ap interface)"
ADDR="$(read_cfg ap address)"
PREFIX="$(read_cfg ap prefix)"
BAND="$(read_cfg ap band)"
CHANNEL="$(read_cfg ap channel)"
COUNTRY="$(read_cfg ap country)"
BLOCK="$(read_cfg ap block_client_internet)"
UPLINK="$(read_cfg uplink interface)"
CON_NAME="viabot-ap"

[[ -n "$SSID" ]] || die "ap.ssid is empty in config/config.yaml"
[[ ${#PSK} -ge 8 ]] || die "ap.password must be at least 8 characters"
[[ "$PSK" != "changeme123" ]] || die "ap.password is still the example default — change it"

step "Wi-Fi access point"
info "interface $IFACE, SSID '$SSID', address $ADDR/$PREFIX, band $BAND ch$CHANNEL"

require_cmd nmcli "Install it with: sudo apt install network-manager"
[[ -d "/sys/class/net/$IFACE" ]] || die "interface $IFACE does not exist"

# The radio is soft-blocked until a regulatory domain is set; without this the
# AP silently fails to come up.
step "Radio regulatory domain"
if command -v raspi-config >/dev/null; then
  sudo raspi-config nonint do_wifi_country "$COUNTRY" >/dev/null 2>&1 || true
fi
sudo iw reg set "$COUNTRY" 2>/dev/null || true
sudo rfkill unblock wifi 2>/dev/null || true

# NetworkManager keeps its own Wi-Fi switch, separate from rfkill. A Pi imaged
# with the wireless step skipped comes up with it off: the radio is present and
# unblocked, but every interface shows "unavailable" and the AP silently never
# starts. Turning it on here is what makes this script work on a fresh Pi.
if [[ "$(nmcli radio wifi 2>/dev/null)" != "enabled" ]]; then
  sudo nmcli radio wifi on
  # The device takes a moment to move out of "unavailable".
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ "$(nmcli -g GENERAL.STATE device show "$IFACE" 2>/dev/null)" == *unavailable* ]] || break
    sleep 1
  done
  ok "enabled NetworkManager's Wi-Fi radio"
else
  ok "NetworkManager Wi-Fi radio already on"
fi
ok "country $COUNTRY, wifi unblocked ($(iw reg get 2>/dev/null | awk '/country/{print $2; exit}'))"

step "NetworkManager connection '$CON_NAME'"
# Recreate rather than patch: a half-edited AP profile is painful to debug at
# the far end of a parking garage.
sudo nmcli connection delete "$CON_NAME" >/dev/null 2>&1 || true
sudo nmcli connection add \
  type wifi ifname "$IFACE" con-name "$CON_NAME" autoconnect yes ssid "$SSID" \
  >/dev/null
sudo nmcli connection modify "$CON_NAME" \
  802-11-wireless.mode ap \
  802-11-wireless.band "$BAND" \
  802-11-wireless.channel "$CHANNEL" \
  802-11-wireless.powersave 2 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.psk "$PSK" \
  ipv4.method shared \
  ipv4.addresses "$ADDR/$PREFIX" \
  ipv6.method disabled \
  connection.autoconnect-priority 100
ok "profile created"

step "Captive-portal DNS"
DNSMASQ_DIR=/etc/NetworkManager/dnsmasq-shared.d
sudo mkdir -p "$DNSMASQ_DIR"
sudo tee "$DNSMASQ_DIR/viabot-captive.conf" >/dev/null <<CONF
# Installed by ViaBot survey rig setup_ap.sh.
# Answer every DNS query from AP clients with the Pi's own address, so any
# connectivity probe reaches our web server and gets redirected to the
# dashboard. This applies only to the shared (wlan0) dnsmasq — the Pi's own
# name resolution over $UPLINK is untouched.
address=/#/$ADDR

# RFC 8910: hand the portal URL to the client directly. Modern iOS and Android
# use this and skip the probe dance entirely.
dhcp-option=114,http://$ADDR/
CONF
ok "wrote $DNSMASQ_DIR/viabot-captive.conf"

step "Client internet policy"
GUARD=/etc/viabot/ap-guard.nft
if [[ "${BLOCK,,}" == "true" || "$BLOCK" == "True" || "$BLOCK" == "1" ]]; then
  sudo mkdir -p /etc/viabot
  # The delete-then-create dance makes reloading this file idempotent.
  sudo tee "$GUARD" >/dev/null <<CONF
#!/usr/sbin/nft -f
# Installed by ViaBot survey rig setup_ap.sh.
# AP clients (your phone) may talk to the Pi, but may not be routed out through
# the cellular modem: their background traffic would otherwise share — and
# distort — the very link being measured, and burn survey data.
table inet viabot
delete table inet viabot
table inet viabot {
  chain forward {
    type filter hook forward priority -10; policy accept;
    iifname "$IFACE" oifname != "$IFACE" drop
  }
}
CONF
  sudo chmod 0644 "$GUARD"
  sudo tee /etc/systemd/system/viabot-ap-guard.service >/dev/null <<UNIT
[Unit]
Description=Keep ViaBot AP clients off the measured cellular uplink
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f $GUARD
ExecStop=-/usr/sbin/nft delete table inet viabot

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now viabot-ap-guard.service
  ok "AP clients are blocked from the cellular uplink"
else
  sudo systemctl disable --now viabot-ap-guard.service >/dev/null 2>&1 || true
  sudo rm -f "$GUARD" /etc/systemd/system/viabot-ap-guard.service
  sudo systemctl daemon-reload
  warn "AP clients CAN reach the internet — phone traffic will share the modem"
fi

step "Bringing the access point up"
sudo nmcli connection up "$CON_NAME" >/dev/null || \
  die "failed to bring up the AP. Try: sudo journalctl -u NetworkManager -n 50"
sleep 2

if iw dev "$IFACE" info 2>/dev/null | grep -q "type AP"; then
  ok "$IFACE is in AP mode"
else
  warn "$IFACE does not report AP mode; check 'nmcli device status'"
fi

ACTUAL="$(ip -4 -o addr show dev "$IFACE" | awk '{print $4}' | head -1)"
[[ -n "$ACTUAL" ]] && ok "address $ACTUAL" || warn "no IPv4 address on $IFACE yet"

step "Uplink sanity check"
if [[ -n "$UPLINK" && -d "/sys/class/net/$UPLINK" ]]; then
  UP_ADDR="$(ip -4 -o addr show dev "$UPLINK" | awk '{print $4}' | head -1)"
  ROUTE_DEV="$(ip route show default | awk '/dev/{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1); exit}')"
  info "$UPLINK address: ${UP_ADDR:-none}"
  info "default route via: ${ROUTE_DEV:-none}"
  if [[ "$ROUTE_DEV" != "$UPLINK" ]]; then
    warn "the default route is not via $UPLINK — measurements may not be going"
    warn "through the router/modem. Check 'ip route'."
  else
    ok "traffic to the internet leaves via $UPLINK as intended"
  fi
else
  warn "uplink interface '$UPLINK' not found"
fi

cat <<SUMMARY

  Access point ready.
      SSID     $SSID
      Dashboard http://$ADDR/

  Join it from your phone; the dashboard should open on its own.
SUMMARY
