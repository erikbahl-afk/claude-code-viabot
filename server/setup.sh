#!/usr/bin/env bash
#
# Set up the ViaBot survey server on a fresh cloud box.
#
#   sudo ./server/setup.sh --domain surveys.example.com
#   sudo ./server/setup.sh --no-tls            # bare IP, see the warning below
#
# Installs and configures both halves:
#   * the report receiver, behind Caddy with an automatic TLS certificate
#   * two authenticated iperf3 servers, one per test direction
#
# Idempotent. Re-run it after a repo update and it will leave your secrets
# alone; pass --rotate-secrets if you actually want new ones.
#
# Tested against Debian 12/13 and Ubuntu 22.04/24.04 on Vultr and Linode.

set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[36m'; OFF=$'\033[0m'
step() { printf '\n%s==>%s %s\n' "$BLUE" "$OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '\n%sERROR:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

DOMAIN=""
USE_TLS=1
ROTATE=0
APP_DIR=/opt/viabot-receiver
DATA_DIR=/var/lib/viabot-receiver
ENV_FILE=/etc/viabot-receiver.env
KEY_DIR=/etc/viabot
SVC_USER=viabot
UPLINK_PORT=5201
DOWNLINK_PORT=5202

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain)          DOMAIN="${2:-}"; shift 2 ;;
    --no-tls)          USE_TLS=0; shift ;;
    --rotate-secrets)  ROTATE=1; shift ;;
    -h|--help)         sed -n '2,20p' "$0"; exit 0 ;;
    *)                 die "unknown option $1" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run this with sudo"
[[ -n "$DOMAIN" || $USE_TLS -eq 0 ]] || die \
  "pass --domain <name> for automatic HTTPS, or --no-tls if you really have no domain"

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$SRC/viabot_receiver.py" ]] || die "run this from the repo: server/setup.sh"

# ---------------------------------------------------------------------------
step "Packages"
export DEBIAN_FRONTEND=noninteractive
PACKAGES=(python3-venv python3-pip iperf3 openssl ca-certificates curl gnupg)
MISSING=()
for package in "${PACKAGES[@]}"; do
  dpkg -s "$package" >/dev/null 2>&1 || MISSING+=("$package")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  apt-get update -qq
  apt-get install -y -qq "${MISSING[@]}"
  ok "installed: ${MISSING[*]}"
else
  ok "already installed"
fi

# ---------------------------------------------------------------------------
step "Service account and directories"
id -u "$SVC_USER" >/dev/null 2>&1 || adduser --system --group --home "$APP_DIR" "$SVC_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$KEY_DIR"
install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$SRC/viabot_receiver.py" "$APP_DIR/"
install -m 0644 -o "$SVC_USER" -g "$SVC_USER" "$SRC/requirements.txt" "$APP_DIR/"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$DATA_DIR"
ok "$APP_DIR and $DATA_DIR ready"

step "Python environment"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  sudo -u "$SVC_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$SVC_USER" "$APP_DIR/.venv/bin/pip" install --quiet --upgrade \
  -r "$APP_DIR/requirements.txt"
ok "dependencies installed"

# ---------------------------------------------------------------------------
# Secrets are generated once and never printed to a log. Re-running setup must
# not change them: the rigs already have them in their config.
step "Secrets"
if [[ -f "$ENV_FILE" && $ROTATE -eq 0 ]]; then
  ok "$ENV_FILE exists — leaving it alone (--rotate-secrets to replace)"
else
  [[ -f "$ENV_FILE" ]] && warn "rotating: every rig will need its config updated"
  UPLOAD_TOKEN="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)"
  VIEWER_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  IPERF_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-32)"
  umask 077
  cat > "$ENV_FILE" <<ENVEOF
VIABOT_RECEIVER_TOKEN=$UPLOAD_TOKEN
VIABOT_RECEIVER_VIEWER_PASSWORD=$VIEWER_PASSWORD
VIABOT_RECEIVER_DATA=$DATA_DIR
VIABOT_IPERF_USER=viabot-rig
VIABOT_IPERF_PASSWORD=$IPERF_PASSWORD
ENVEOF
  chmod 600 "$ENV_FILE"
  ok "generated; they are printed once at the end and never again"
fi
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

# ---------------------------------------------------------------------------
step "iperf3 credentials"
if [[ ! -f "$KEY_DIR/iperf3_private.pem" || $ROTATE -eq 1 ]]; then
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
    -out "$KEY_DIR/iperf3_private.pem" -outform PEM 2>/dev/null
  openssl rsa -in "$KEY_DIR/iperf3_private.pem" -outform PEM -pubout \
    -out "$KEY_DIR/iperf3_public.pem" 2>/dev/null
  ok "key pair generated"
else
  ok "key pair already present"
fi
# The credentials file is derived from the env file, so it is always in step
# with it even if only one of the two was rotated.
printf '%s,%s\n' "$VIABOT_IPERF_USER" \
  "$(printf '{%s}%s' "$VIABOT_IPERF_USER" "$VIABOT_IPERF_PASSWORD" \
     | sha256sum | awk '{print $1}')" > "$KEY_DIR/iperf3_users.csv"
chmod 600 "$KEY_DIR/iperf3_private.pem" "$KEY_DIR/iperf3_users.csv"
chmod 644 "$KEY_DIR/iperf3_public.pem"
chown -R "$SVC_USER:$SVC_USER" "$KEY_DIR"
ok "authorised user '$VIABOT_IPERF_USER'"

# ---------------------------------------------------------------------------
step "Services"
install -m 0644 "$SRC/viabot-receiver.service" /etc/systemd/system/
install -m 0644 "$SRC/viabot-iperf3@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now viabot-receiver >/dev/null
# Two instances: one iperf3 server runs one test at a time, and the rig
# measures both directions at once.
systemctl enable --now "viabot-iperf3@$UPLINK_PORT" >/dev/null
systemctl enable --now "viabot-iperf3@$DOWNLINK_PORT" >/dev/null
ok "receiver and both iperf3 servers started"

# ---------------------------------------------------------------------------
step "TLS"
if [[ $USE_TLS -eq 1 ]]; then
  if ! command -v caddy >/dev/null 2>&1; then
    KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor -o "$KEYRING"
    # An empty keyring is the failure that does not announce itself: apt goes
    # on to report the repository as unsigned, which reads like the vendor's
    # problem rather than a missing gpg here.
    [[ -s "$KEYRING" ]] || die "could not build $KEYRING — is gnupg installed?"
    chmod 0644 "$KEYRING"

    # The sources line is written here rather than piped from the vendor's
    # generated file. That file's shape is theirs to change, and if it omits
    # signed-by then apt looks in the system trust store, never sees the key
    # just placed above, and rejects the repository as unsigned.
    printf 'deb [signed-by=%s] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main\n' \
      "$KEYRING" > /etc/apt/sources.list.d/caddy-stable.list

    apt-get update -qq
    apt-get install -y -qq caddy \
      || die "caddy would not install. Check the output above; the rest of the
    server is already set up, so fixing this and re-running is safe."
  fi
  command -v caddy >/dev/null 2>&1 \
    || die "caddy is not installed, so there is nothing to terminate TLS."
  mkdir -p /etc/caddy
  cat > /etc/caddy/Caddyfile <<CADDYEOF
# Terminates TLS and forwards to the receiver, which listens on localhost only.
$DOMAIN {
    reverse_proxy 127.0.0.1:8089
    # A full walk video is a few hundred megabytes and arrives in chunks; do
    # not let the proxy truncate one.
    request_body {
        max_size 0
    }
}
CADDYEOF
  systemctl enable --now caddy >/dev/null
  systemctl reload caddy 2>/dev/null || systemctl restart caddy

  # Undo a previous --no-tls run. That one binds the receiver to every
  # interface because nothing is proxying for it; leaving that in place behind
  # Caddy would keep port 8089 answering in the clear, so the TLS everyone
  # believes is protecting the reports would be one URL away from bypassed.
  if [[ -f /etc/systemd/system/viabot-receiver.service.d/override.conf ]]; then
    rm -f /etc/systemd/system/viabot-receiver.service.d/override.conf
    rmdir /etc/systemd/system/viabot-receiver.service.d 2>/dev/null || true
    systemctl daemon-reload
    systemctl restart viabot-receiver
    ok "receiver moved back behind the proxy, off the public interface"
  fi
  BASE_URL="https://$DOMAIN"
  ok "Caddy will fetch a certificate for $DOMAIN on first request"
else
  IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  BASE_URL="http://$IP:8089"
  warn "no TLS: the upload token and the viewer password will cross the"
  warn "internet in clear text, and reports will not be private."
  warn "Get a domain name and re-run with --domain <name>."
  # Without a proxy the receiver has to listen on the public interface.
  mkdir -p /etc/systemd/system/viabot-receiver.service.d
  cat > /etc/systemd/system/viabot-receiver.service.d/override.conf <<OVEOF
[Service]
ExecStart=
ExecStart=$APP_DIR/.venv/bin/python viabot_receiver.py --host 0.0.0.0 --port 8089
OVEOF
  systemctl daemon-reload && systemctl restart viabot-receiver
fi

# ---------------------------------------------------------------------------
step "Verifying"
sleep 2
systemctl is-active --quiet viabot-receiver && ok "receiver running" \
  || die "receiver failed: journalctl -u viabot-receiver -n 30"
for port in "$UPLINK_PORT" "$DOWNLINK_PORT"; do
  systemctl is-active --quiet "viabot-iperf3@$port" && ok "iperf3 on $port running" \
    || warn "iperf3 on $port is not running: journalctl -u viabot-iperf3@$port -n 20"
done
curl -fsS --max-time 5 "http://127.0.0.1:8089/healthz" >/dev/null \
  && ok "receiver answered /healthz" || warn "receiver did not answer locally"

# ---------------------------------------------------------------------------
printf '\n%s==>%s %sSetup complete.%s\n' "$BLUE" "$OFF" "$GREEN" "$OFF"
cat <<SUMMARY

  Open the firewall for these, in your provider's control panel:

      TCP 80, 443           the report pages (Caddy needs 80 to get a cert)
      TCP and UDP $UPLINK_PORT      uplink test
      TCP and UDP $DOWNLINK_PORT      downlink test

  iperf3 negotiates over TCP and sends the test over UDP, so it needs both.

  Copy $KEY_DIR/iperf3_public.pem to the rig — it is a public key, so
  email or a paste is fine — then put this in config/config.yaml on the rig:

publish:
  enabled: true
  url: "$BASE_URL"
  token: "$VIABOT_RECEIVER_TOKEN"

udp_load:
  enabled: true
  server: "${DOMAIN:-$BASE_URL}"
  username: "$VIABOT_IPERF_USER"
  password: "$VIABOT_IPERF_PASSWORD"
  public_key_path: "/home/viabot/claude-code-viabot/config/iperf3_public.pem"

  To read the reports in a browser, sign in with any username and this
  password:

      $VIABOT_RECEIVER_VIEWER_PASSWORD

  Write these down now. They are in $ENV_FILE (root only) and
  this is the only time the script prints them.

SUMMARY
