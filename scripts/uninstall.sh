#!/usr/bin/env bash
#
# Remove everything setup.sh installed on the system. Leaves the repository,
# config/config.yaml and data/ alone.
#
#   ./scripts/uninstall.sh [--keep-ap]

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

KEEP_AP=0
[[ "${1:-}" == "--keep-ap" ]] && KEEP_AP=1

step "Stopping services"
for unit in viabot-survey.service viabot-update.service viabot-ap-guard.service; do
  sudo systemctl disable --now "$unit" >/dev/null 2>&1 || true
  sudo rm -f "/etc/systemd/system/$unit"
  ok "removed $unit"
done
sudo systemctl daemon-reload

step "Removing privilege grants"
sudo rm -f /etc/sudoers.d/viabot-survey
ok "removed /etc/sudoers.d/viabot-survey"

if [[ "$KEEP_AP" -eq 1 ]]; then
  step "Wi-Fi access point"
  warn "left in place (--keep-ap)"
else
  step "Wi-Fi access point"
  sudo nmcli connection delete viabot-ap >/dev/null 2>&1 || true
  sudo rm -f /etc/NetworkManager/dnsmasq-shared.d/viabot-captive.conf
  sudo rm -rf /etc/viabot
  sudo nft delete table inet viabot >/dev/null 2>&1 || true
  sudo systemctl reload NetworkManager >/dev/null 2>&1 || true
  ok "AP profile, captive DNS and firewall rule removed"
fi

cat <<SUMMARY

  Uninstalled. The repository, config/config.yaml and data/ are untouched.
  Delete them yourself if you want a clean slate.

SUMMARY
