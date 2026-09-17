#!/usr/bin/env bash
#
# How much can this link actually carry, right here?
#
#   ./scripts/capacity_test.sh                  # 10 s each way
#   ./scripts/capacity_test.sh --seconds 20
#   ./scripts/capacity_test.sh --udp 5M         # also: does 5 Mbit/s get through?
#
# Everything else on this rig measures a link under a *fixed* teleop-sized
# load and asks whether it kept up. Nothing measures the ceiling, so nothing
# can answer "could this spot carry a heavier stream than the one we test
# with" — which is exactly the question when someone says teleop needs 5
# Mbit/s up rather than the 650 kbit/s we measured.
#
# This saturates the link deliberately, which is why it is a one-off command
# and not a worker. Do not run it during a walk: it would flood the same
# uplink ping is measuring and manufacture dead zones. The script refuses.
#
# --udp is the direct answer to "is N enough here": it sends a constant N
# both ways, like a real teleop stream, and reports what arrived.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROOT="$(repo_root)"
CONFIG="$ROOT/config/config.yaml"
EXAMPLE="$ROOT/config/config.example.yaml"
SECONDS_EACH=10
UDP_RATE=""
PORT=80

while [[ $# -gt 0 ]]; do
  case "$1" in
    --seconds) SECONDS_EACH="${2:-10}"; shift 2 ;;
    --udp)     UDP_RATE="${2:-}"; shift 2 ;;
    --port)    PORT="${2:-80}"; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *)         die "unknown option $1" ;;
  esac
done

require_cmd iperf3 "apt-get install -y iperf3"

# ---------------------------------------------------------------------------
# Configuration. The example file is the default layer, so read it first and
# let the real config override — the same order the app merges them in.
# ---------------------------------------------------------------------------
cfg() {
  local value
  value="$(yaml_get "$CONFIG" "$1" "$2")"
  [[ -n "$value" ]] || value="$(yaml_get "$EXAMPLE" "$1" "$2")"
  printf '%s' "$value"
}

SERVER="$(cfg udp_load server)"
UP_PORT="$(cfg udp_load uplink_port)"
DOWN_PORT="$(cfg udp_load downlink_port)"
USERNAME="$(cfg udp_load username)"
PUBLIC_KEY="$(cfg udp_load public_key_path)"
CONFIGURED_UP="$(cfg udp_load uplink_bitrate)"
CONFIGURED_DOWN="$(cfg udp_load downlink_bitrate)"
IFACE="$(cfg uplink interface)"

# Never printed, never passed on the command line — iperf3 reads it from the
# environment so it cannot show up in `ps` for anyone else on the box.
IPERF3_PASSWORD="$(cfg udp_load password)"
export IPERF3_PASSWORD

[[ -n "$SERVER" && "$SERVER" != "None" && "$SERVER" != "null" ]] \
  || die "no udp_load.server in $CONFIG. Set it to the survey server's hostname."

# ---------------------------------------------------------------------------
# Refuse to run during a walk. This test exists to saturate the uplink, and
# ping — which is what dead zones are detected from — is sharing it.
# ---------------------------------------------------------------------------
step "checking that no survey is running"
STATUS="$(curl -fsS --max-time 4 "http://127.0.0.1:$PORT/api/status" 2>/dev/null || true)"
# The status payload carries "run": null when idle and a run object otherwise,
# including while paused — a paused walk can be resumed on top of this test.
ACTIVE="$(printf '%s' "$STATUS" | python3 -c \
  'import json,sys; print("yes" if json.load(sys.stdin).get("run") else "no")' \
  2>/dev/null || true)"
case "$ACTIVE" in
  yes) die "a survey run is in progress. End it first — saturating the uplink
    now would flood the link ping is measuring and invent dead zones in
    the results." ;;
  no)  ok "no run in progress" ;;
  *)   warn "could not read the rig's status on port $PORT — carrying on."
       info "If a walk is in fact running, stop it: this test would corrupt it." ;;
esac

# ---------------------------------------------------------------------------
# The measured link is eth0, not the Wi-Fi you are connected over. iperf3's
# -B wants an address rather than an interface name, so resolve it.
# ---------------------------------------------------------------------------
BIND=()
if [[ -n "$IFACE" ]]; then
  # pipefail is on, so a missing `ip` would take the whole script down here.
  ADDRESS="$(ip -o -4 addr show dev "$IFACE" 2>/dev/null \
             | awk '{print $4}' | cut -d/ -f1 | head -1 || true)"
  if [[ -n "$ADDRESS" ]]; then
    BIND=(-B "$ADDRESS")
    ok "measuring through $IFACE ($ADDRESS)"
  else
    warn "$IFACE has no IPv4 address; letting the routing table choose."
    info "Check the modem is up: ip -4 addr show dev $IFACE"
  fi
fi

# ---------------------------------------------------------------------------
# iperf3 signs every test with a timestamp and rejects a client whose clock is
# more than ~10 s out — with the same message a wrong password gets. This Pi
# has no RTC, so that is a real possibility and not a theoretical one. Read the
# server's clock off an HTTPS response header, which needs no credentials and
# works even when it answers 401.
# ---------------------------------------------------------------------------
step "checking the clock against the server"
SERVER_DATE="$(curl -sI --max-time 6 "https://$SERVER" 2>/dev/null \
               | tr -d '\r' \
               | awk 'tolower($1) == "date:" { sub(/^[^ ]+ /, ""); print; exit }' \
               || true)"
CLOCK_SKEW_S=""
if [[ -n "$SERVER_DATE" ]]; then
  CLOCK_SKEW_S="$(SERVER_DATE="$SERVER_DATE" python3 - <<'PY' 2>/dev/null || true
import email.utils, os, time
stamp = email.utils.parsedate_to_datetime(os.environ["SERVER_DATE"])
print(round(time.time() - stamp.timestamp()))
PY
)"
fi
if [[ -z "$CLOCK_SKEW_S" ]]; then
  warn "could not read the server's clock — carrying on."
elif (( CLOCK_SKEW_S > 10 || CLOCK_SKEW_S < -10 )); then
  warn "the rig's clock is ${CLOCK_SKEW_S}s off the server's."
  info "iperf3 will refuse to authenticate. Fix it first:"
  info "  sudo systemctl restart systemd-timesyncd && timedatectl"
else
  ok "clock is within ${CLOCK_SKEW_S}s of the server"
fi
export CLOCK_SKEW_S

AUTH=()
if [[ -n "$USERNAME" && -n "$PUBLIC_KEY" ]]; then
  [[ -f "$PUBLIC_KEY" ]] || die "public_key_path points at $PUBLIC_KEY, which does not exist.
    Copy iperf3_public.pem from the server — see server/README.md."
  AUTH=(--username "$USERNAME" --rsa-public-key-path "$PUBLIC_KEY")
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
# Each test writes JSON, which is parsed rather than screen-scraped: iperf3's
# human output changes shape between versions and this has to keep working on
# whatever the Pi has installed.
# ---------------------------------------------------------------------------
run_test() {                     # label port file extra-args...
  local label="$1" port="$2" file="$3"; shift 3
  info "$label ..."
  iperf3 -c "$SERVER" -p "$port" -J -t "$SECONDS_EACH" \
         "${BIND[@]}" "${AUTH[@]}" "$@" > "$file" 2>"$file.err" || true
  if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$file" 2>/dev/null; then
    warn "$label produced no usable result"
    sed -n '1,3p' "$file.err" | while read -r line; do info "$line"; done
    return 1
  fi
}

step "measuring the ceiling (TCP, uncapped, ${SECONDS_EACH}s each way)"
run_test "uplink   — the rig sending, the direction that carries robot video" \
         "$UP_PORT" "$WORK/up.json" || true
run_test "downlink — the rig receiving" \
         "$DOWN_PORT" "$WORK/down.json" -R || true

if [[ -n "$UDP_RATE" ]]; then
  step "constant $UDP_RATE both ways, the way a teleop stream behaves"
  # 1200-byte datagrams, matching udp_load: iperf3's 32 KB default fragments
  # into two dozen packets and reads several times worse than real video does.
  run_test "uplink   at $UDP_RATE" "$UP_PORT" "$WORK/uup.json" \
           -u -b "$UDP_RATE" -l 1200 || true
  run_test "downlink at $UDP_RATE" "$DOWN_PORT" "$WORK/udown.json" \
           -u -b "$UDP_RATE" -l 1200 -R || true
fi

step "result"
CONFIGURED_UP="$CONFIGURED_UP" CONFIGURED_DOWN="$CONFIGURED_DOWN" \
UDP_RATE="$UDP_RATE" CLOCK_SKEW_S="$CLOCK_SKEW_S" \
PYTHONPATH="$ROOT" python3 -m viabot_survey.capacity "$WORK"
