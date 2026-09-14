#!/usr/bin/env bash
#
# Does the rig cook itself inside a closed case?
#
#   ./scripts/thermal_test.sh                 # one hour, sampling every 30 s
#   ./scripts/thermal_test.sh --minutes 90
#
# Foam insulates and a closed case has no airflow, so this has to be measured
# rather than assumed — and measured under load, with the camera encoding and
# the modem working, because an idle rig in a closed case proves nothing.
#
# Start a survey run first. The script says so if you have not.
#
# Leave it running and walk away: it survives the SSH session dropping, and
# writes a log you can read afterwards either way.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROOT="$(repo_root)"
MINUTES=60
INTERVAL=30
PORT=80

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minutes)  MINUTES="${2:-60}"; shift 2 ;;
    --interval) INTERVAL="${2:-30}"; shift 2 ;;
    --port)     PORT="${2:-80}"; shift 2 ;;
    -h|--help)  sed -n '2,18p' "$0"; exit 0 ;;
    *)          die "unknown option $1" ;;
  esac
done

require_cmd vcgencmd "Install libraspberrypi-bin, or run this on the Pi."

#: A Pi 4 starts reducing its clock at 80 °C and gets serious about it at 85.
#: Staying under 70 leaves room for a hot garage on a summer afternoon, which
#: is the condition this rig will actually meet and the bench never will.
SOFT_LIMIT=80
COMFORT=70

LOG="$ROOT/data/thermal-$(date +%Y%m%dT%H%M%S).log"
mkdir -p "$(dirname "$LOG")"

# -- what the bits in get_throttled actually mean ----------------------------
# The hex is unreadable and the distinction that matters is buried in it:
# the low bits are happening *now*, the high bits happened at some point since
# boot. A rig that threw a flag an hour ago and recovered still failed.
decode_throttled() {
  local value=$((${1:-0}))
  local -a now=() ever=()
  (( value & 0x1 ))     && now+=("undervoltage")
  (( value & 0x2 ))     && now+=("arm-capped")
  (( value & 0x4 ))     && now+=("throttled")
  (( value & 0x8 ))     && now+=("soft-temp-limit")
  (( value & 0x10000 )) && ever+=("undervoltage")
  (( value & 0x20000 )) && ever+=("arm-capped")
  (( value & 0x40000 )) && ever+=("throttled")
  (( value & 0x80000 )) && ever+=("soft-temp-limit")
  printf 'now=%s since-boot=%s' \
    "$([[ ${#now[@]} -eq 0 ]] && echo none || (IFS=,; echo "${now[*]}"))" \
    "$([[ ${#ever[@]} -eq 0 ]] && echo none || (IFS=,; echo "${ever[*]}"))"
}

cpu_temp()  { vcgencmd measure_temp | tr -dc '0-9.'; }
cpu_clock() { awk -v hz="$(vcgencmd measure_clock arm | cut -d= -f2)" 'BEGIN{printf "%.0f", hz/1000000}'; }
throttled() { vcgencmd get_throttled | cut -d= -f2; }

run_state() {
  local body
  body="$(curl -s --max-time 3 "http://127.0.0.1:$PORT/api/status" 2>/dev/null)" || return 1
  printf '%s' "$body" | "$ROOT/.venv/bin/python" -c "
import json, sys
try:
    s = json.load(sys.stdin)
except Exception:
    print('unknown'); raise SystemExit
run = s.get('run') or {}
cam = (s.get('workers') or {}).get('camera') or {}
print(('recording' if cam.get('recording') else 'run-active') if run.get('id')
      else 'idle')
" 2>/dev/null || printf 'unknown'
}

# ---------------------------------------------------------------------------
step "Thermal test"
info "logging to $LOG"
info "$MINUTES minutes, a sample every ${INTERVAL}s"

STATE="$(run_state || echo unreachable)"
case "$STATE" in
  recording)  ok "a run is active and the camera is recording — this is the real load" ;;
  run-active) warn "a run is active but the camera is not recording; check the dashboard" ;;
  idle)       warn "NO RUN IS ACTIVE. An idle rig in a closed case proves nothing:"
              warn "the camera encoding is most of the heat. Start a run from the"
              warn "phone, then re-run this." ;;
  *)          warn "could not reach the dashboard on port $PORT; carrying on anyway" ;;
esac

printf '# started %s\n# %s minutes, %ss interval, state at start: %s\n' \
  "$(date -Is)" "$MINUTES" "$INTERVAL" "$STATE" > "$LOG"
printf '# iso\tsecs\ttemp_c\tclock_mhz\tstate\tthrottled\n' >> "$LOG"

END=$(( $(date +%s) + MINUTES * 60 ))
START=$(date +%s)
MAX=0
HOT_SAMPLES=0
WARM_SAMPLES=0
SAMPLES=0

printf '\n    %-8s %-8s %-9s %-11s %s\n' "elapsed" "temp" "clock" "state" "throttling"
while [[ $(date +%s) -lt $END ]]; do
  NOW=$(date +%s)
  ELAPSED=$(( NOW - START ))
  TEMP="$(cpu_temp)"
  CLOCK="$(cpu_clock)"
  RAW="$(throttled)"
  STATE="$(run_state || echo unreachable)"
  DECODED="$(decode_throttled "$RAW")"

  printf '%s\t%s\t%s\t%s\t%s\t%s %s\n' \
    "$(date -Is)" "$ELAPSED" "$TEMP" "$CLOCK" "$STATE" "$RAW" "$DECODED" >> "$LOG"
  printf '    %-8s %-8s %-9s %-11s %s\n' \
    "$(printf '%dm%02ds' $((ELAPSED/60)) $((ELAPSED%60)))" \
    "${TEMP}°C" "${CLOCK} MHz" "$STATE" "$DECODED"

  SAMPLES=$(( SAMPLES + 1 ))
  awk -v t="$TEMP" -v m="$MAX" 'BEGIN{exit !(t>m)}' && MAX="$TEMP"
  awk -v t="$TEMP" -v l="$SOFT_LIMIT" 'BEGIN{exit !(t>=l)}' && HOT_SAMPLES=$(( HOT_SAMPLES + 1 ))
  awk -v t="$TEMP" -v c="$COMFORT" 'BEGIN{exit !(t>=c)}' && WARM_SAMPLES=$(( WARM_SAMPLES + 1 ))

  sleep "$INTERVAL"
done

# ---------------------------------------------------------------------------
step "Result"
FINAL_RAW="$(throttled)"
# Whole minutes would round a throttling episode shorter than the sample
# interval down to "0 min", which reads as "it never happened".
duration() { printf '%dm%02ds' $(( $1 / 60 )) $(( $1 % 60 )); }

info "peak temperature: ${MAX} °C over $SAMPLES samples"
info "time at or above ${COMFORT} °C: $(duration $(( WARM_SAMPLES * INTERVAL ))) ($WARM_SAMPLES samples)"
info "time at or above ${SOFT_LIMIT} °C: $(duration $(( HOT_SAMPLES * INTERVAL ))) ($HOT_SAMPLES samples)"
info "throttling flags at the end: $FINAL_RAW $(decode_throttled "$FINAL_RAW")"

printf '\n'
if [[ "$FINAL_RAW" != "0x0" ]]; then
  warn "The Pi flagged something. Anything under 'since-boot' happened during"
  warn "this test even if it has recovered — a rig that throttled for ten"
  warn "minutes in the middle of a walk produced ten minutes of suspect data."
  warn "If it says undervoltage, that is the power splice, not the case."
elif awk -v m="$MAX" -v l="$SOFT_LIMIT" 'BEGIN{exit !(m>=l)}'; then
  warn "Peaked at ${MAX} °C with no flag raised — that is close enough to the"
  warn "${SOFT_LIMIT} °C limit that a warm garage would push it over. Vent the case."
elif awk -v m="$MAX" -v c="$COMFORT" 'BEGIN{exit !(m>=c)}'; then
  warn "Peaked at ${MAX} °C. No throttling, but little headroom for a hot day."
  warn "Consider vent holes before a summer survey."
else
  ok "Peaked at ${MAX} °C with no throttling — comfortable margin."
  ok "The closed case is fine for a walk in conditions like today's."
fi

printf '\n'
info "full log: $LOG"
info "send it with: ./scripts/collect.sh   (or just paste the summary above)"
