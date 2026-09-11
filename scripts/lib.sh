# Shared helpers for the ViaBot survey rig scripts. Sourced, not executed.

set -euo pipefail

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BLUE=$'\033[36m'; OFF=$'\033[0m'

step() { printf '\n%s==>%s %s\n' "$BLUE" "$OFF" "$*"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$OFF" "$*" >&2; }
die()  { printf '\n%sERROR:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

# Absolute path to the repository root, however the script was invoked.
repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
}

# The unprivileged account that owns the repo and runs the service. When setup
# is run under sudo we want the human's account, not root.
service_user() {
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    echo "$SUDO_USER"
  else
    id -un
  fi
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command '$1' not found. $2"
}

# Read a value out of a YAML file without a YAML parser being installed yet.
# Only handles the flat "  key: value" shapes this project uses.
yaml_get() {
  local file="$1" section="$2" key="$3"
  python3 - "$file" "$section" "$key" <<'PY' 2>/dev/null || true
import sys
try:
    import yaml
except ImportError:
    sys.exit(1)
with open(sys.argv[1]) as fh:
    data = yaml.safe_load(fh) or {}
value = (data.get(sys.argv[2]) or {}).get(sys.argv[3])
if value is not None:
    print(value)
PY
}
