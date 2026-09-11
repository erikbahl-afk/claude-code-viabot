#!/usr/bin/env bash
#
# Copy a run's data off the rig onto the machine you are sitting at.
# Run this from your LAPTOP, not from the Pi.
#
#   ./scripts/collect.sh 20260911-143000-sunset-l2
#   ./scripts/collect.sh --list
#
# The Pi must be reachable — plug your laptop into the router's spare LAN port.

set -euo pipefail

HOST="${VIABOT_HOST:-viabot@garage-surveyor-01.local}"
REMOTE_DIR="${VIABOT_REMOTE_DIR:-~/claude-code-viabot/data}"
DEST="${VIABOT_DEST:-./survey-data}"

if [[ "${1:-}" == "--list" || $# -eq 0 ]]; then
  echo "Runs available on $HOST:"
  ssh "$HOST" "ls -1 $REMOTE_DIR/video 2>/dev/null" || {
    echo "Could not list runs. Check that $HOST is reachable (ssh $HOST)." >&2
    exit 1
  }
  echo
  echo "Then: $0 <run-id>"
  exit 0
fi

RUN_ID="$1"
mkdir -p "$DEST/$RUN_ID"

echo "==> Video segments"
scp -r "$HOST:$REMOTE_DIR/video/$RUN_ID" "$DEST/" || {
  echo "No video for $RUN_ID (camera may have been disabled)." >&2
}

echo "==> Measurements"
# The CSV exports come from the running dashboard so the columns match the UI.
PI_HOST="${HOST#*@}"
for what in samples marks; do
  if curl -fsS --max-time 30 \
       "http://$PI_HOST/api/runs/$RUN_ID/$what.csv" -o "$DEST/$RUN_ID/$what.csv"; then
    echo "    saved $DEST/$RUN_ID/$what.csv"
  else
    echo "    could not fetch $what.csv over HTTP; copying the database instead" >&2
    scp "$HOST:$REMOTE_DIR/surveys.db" "$DEST/$RUN_ID/surveys.db" || true
  fi
done

echo
echo "Done: $DEST/$RUN_ID"
