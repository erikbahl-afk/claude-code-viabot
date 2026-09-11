#!/usr/bin/env bash
#
# Pull the latest code and restart the rig.
#
#   ./scripts/update.sh
#
# Normally you do not run this by hand — press "Apply update" on the dashboard,
# which starts viabot-update.service, which runs this script. Doing it that way
# means the restart does not kill the process that asked for it.
#
# This is a hard reset onto the tracked branch: local edits to tracked files are
# discarded. config/config.yaml and data/ are gitignored and survive.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ROOT="$(repo_root)"
cd "$ROOT"

CONFIG="$ROOT/config/config.yaml"
REMOTE="origin"; BRANCH="main"
if [[ -f "$CONFIG" ]]; then
  R="$(yaml_get "$CONFIG" update remote)"; [[ -n "$R" ]] && REMOTE="$R"
  B="$(yaml_get "$CONFIG" update branch)"; [[ -n "$B" ]] && BRANCH="$B"
fi

step "Updating from $REMOTE/$BRANCH"
BEFORE="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
info "currently at ${BEFORE:0:8}"

# The rig pulls over the cellular link, which is exactly the thing that might be
# having a bad day; retry rather than leave the rig half-updated.
FETCHED=0
for attempt in 1 2 3 4; do
  if git fetch --prune "$REMOTE" "$BRANCH"; then FETCHED=1; break; fi
  DELAY=$((2 ** attempt))
  warn "fetch failed (attempt $attempt), retrying in ${DELAY}s"
  sleep "$DELAY"
done
[[ "$FETCHED" -eq 1 ]] || die "could not fetch from $REMOTE — is the cellular link up?"

AFTER="$(git rev-parse "$REMOTE/$BRANCH")"
if [[ "$BEFORE" == "$AFTER" ]]; then
  ok "already up to date at ${BEFORE:0:8}"
else
  info "updating to ${AFTER:0:8}"
  git log --oneline --no-decorate "HEAD..$REMOTE/$BRANCH" | sed 's/^/      /'
  git reset --hard "$REMOTE/$BRANCH"
  ok "checked out ${AFTER:0:8}"
fi

step "Dependencies"
if [[ -x "$ROOT/.venv/bin/pip" ]]; then
  "$ROOT/.venv/bin/pip" install --quiet --upgrade -r "$ROOT/requirements.txt"
  ok "dependencies up to date"
else
  warn ".venv missing — run ./scripts/setup.sh"
fi

step "Restarting the service"
if systemctl list-unit-files viabot-survey.service >/dev/null 2>&1; then
  # -n: never prompt for a password. setup.sh installs the matching sudoers rule.
  if sudo -n systemctl restart viabot-survey.service; then
    ok "viabot-survey restarted"
  else
    warn "could not restart automatically; run: sudo systemctl restart viabot-survey"
  fi
else
  warn "viabot-survey.service is not installed — run ./scripts/setup.sh"
fi

step "Done"
info "was ${BEFORE:0:8}  now $(git rev-parse --short HEAD)"
