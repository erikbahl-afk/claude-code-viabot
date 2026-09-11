"""Configuration loading.

Precedence, lowest to highest:

1. the defaults baked into ``config/config.example.yaml``
2. ``config/config.yaml`` (gitignored; holds secrets)
3. ``VIABOT_<SECTION>_<KEY>`` environment variables

Keeping the example file as the default layer means a config written against an
older version of the rig still boots after an update that adds new keys.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
EXAMPLE_PATH = CONFIG_DIR / "config.example.yaml"
LOCAL_PATH = CONFIG_DIR / "config.yaml"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(raw: str, reference: Any) -> Any:
    """Parse an env-var string into the type the reference value suggests."""
    if isinstance(reference, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ValueError(f"expected a boolean, got {raw!r}")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(raw)
    if isinstance(reference, float):
        return float(raw)
    if isinstance(reference, (list, dict)) or reference is None:
        # Lists, mappings and "unset" values are ambiguous as bare strings, so
        # take YAML's reading of them (also gives us null/true/numbers free).
        return yaml.safe_load(raw)
    return raw


def _apply_env(data: dict, environ: dict[str, str]) -> list[str]:
    """Overlay VIABOT_SECTION_KEY variables onto ``data``. Returns warnings."""
    warnings: list[str] = []
    # Longest section name first so that e.g. a hypothetical "iperf3_extra"
    # section wins over "iperf3" for VIABOT_IPERF3_EXTRA_*.
    sections = sorted(data, key=len, reverse=True)
    for name, raw in environ.items():
        if not name.startswith("VIABOT_"):
            continue
        remainder = name[len("VIABOT_"):].lower()
        for section in sections:
            prefix = f"{section}_"
            if not remainder.startswith(prefix):
                continue
            key = remainder[len(prefix):]
            if not isinstance(data[section], dict) or key not in data[section]:
                warnings.append(f"ignoring {name}: no '{key}' key in section '{section}'")
                break
            try:
                data[section][key] = _coerce(raw, data[section][key])
            except (ValueError, yaml.YAMLError) as exc:
                warnings.append(f"ignoring {name}: {exc}")
            break
        else:
            warnings.append(f"ignoring {name}: no matching config key")
    return warnings


class Config:
    """Read-only view over the merged configuration."""

    def __init__(self, data: dict, *, source: Path | None = None,
                 warnings: list[str] | None = None) -> None:
        self._data = data
        self.source = source
        self.warnings = warnings or []

    def __getitem__(self, section: str) -> dict:
        return self._data[section]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, default)

    def as_dict(self) -> dict:
        return copy.deepcopy(self._data)

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def data_dir(self) -> Path:
        """Absolute survey-output directory, created on demand."""
        raw = Path(self.get("storage", "data_dir", "data")).expanduser()
        path = raw if raw.is_absolute() else REPO_ROOT / raw
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "surveys.db"

    @property
    def video_dir(self) -> Path:
        path = self.data_dir / "video"
        path.mkdir(parents=True, exist_ok=True)
        return path


def load(path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
    """Load configuration, merging example defaults, local file and env vars."""
    with EXAMPLE_PATH.open() as handle:
        data = yaml.safe_load(handle) or {}

    local = LOCAL_PATH if path is None else path
    source: Path | None = None
    if local.exists():
        with local.open() as handle:
            overlay = yaml.safe_load(handle) or {}
        if not isinstance(overlay, dict):
            raise ValueError(f"{local} must contain a YAML mapping")
        data = _deep_merge(data, overlay)
        source = local

    warnings = _apply_env(data, os.environ if environ is None else environ)
    return Config(data, source=source, warnings=warnings)


# Keys scrubbed before a config tree is stored or served. This repository is
# public and the dashboard is reachable by anyone within Wi-Fi range, so the
# passphrase must never leave the process — not into the database, not into an
# API response.
SECRET_KEYS = {"password", "psk", "passphrase", "secret", "token"}


def redact(value: Any) -> Any:
    """Deep-copy a config tree with secret-looking values masked."""
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            if str(key).lower() in SECRET_KEYS:
                out[key] = "********" if inner else ""
            else:
                out[key] = redact(inner)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
