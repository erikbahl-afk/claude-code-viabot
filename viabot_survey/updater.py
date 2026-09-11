"""Pull new code from GitHub over the cellular link.

The intended workflow (see docs/WORKFLOW.md): Claude pushes a branch and opens
a pull request, you press Merge on github.com, then you press "Apply update" on
the rig's own dashboard. No terminal required.

Applying an update restarts the service, which kills the web request that asked
for it — so the actual work is handed to ``scripts/update.sh`` running detached
under systemd, and the browser polls :func:`status` until the app comes back.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


class UpdateError(RuntimeError):
    pass


class Updater:
    def __init__(self, repo_root: Path, remote: str = "origin", branch: str = "main",
                 enabled: bool = True) -> None:
        self.repo_root = Path(repo_root)
        self.remote = remote
        self.branch = branch
        self.enabled = enabled
        self._last_fetch: float | None = None
        self._last_error: str | None = None
        self._applying_since: float | None = None

    # -- git plumbing --------------------------------------------------------

    def _git(self, *args: str, timeout: float = 120.0) -> str:
        if shutil.which("git") is None:
            raise UpdateError("git is not installed")
        try:
            result = subprocess.run(
                ["git", *args], cwd=self.repo_root, capture_output=True,
                text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"git {args[0]} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise UpdateError(str(exc)) from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip().splitlines()
            raise UpdateError(message[-1] if message else f"git {args[0]} failed")
        return result.stdout.strip()

    def current_commit(self) -> str | None:
        try:
            return self._git("rev-parse", "HEAD")
        except UpdateError:
            return None

    def current_describe(self) -> str | None:
        try:
            return self._git("log", "-1", "--format=%h %s")
        except UpdateError:
            return None

    def is_dirty(self) -> bool:
        """True when tracked files have local edits.

        An update hard-resets the working tree, so the UI warns before throwing
        away hand edits made over SSH.
        """
        try:
            return bool(self._git("status", "--porcelain", "--untracked-files=no"))
        except UpdateError:
            return False

    # -- checking ------------------------------------------------------------

    def fetch(self) -> None:
        self._git("fetch", self.remote, self.branch, timeout=180)
        self._last_fetch = time.time()

    def remote_commit(self) -> str | None:
        try:
            return self._git("rev-parse", f"{self.remote}/{self.branch}")
        except UpdateError:
            return None

    def pending_commits(self, limit: int = 20) -> list[str]:
        """One-line summaries of what an update would bring in."""
        try:
            output = self._git(
                "log", "--oneline", "--no-decorate", f"-{limit}",
                f"HEAD..{self.remote}/{self.branch}")
        except UpdateError:
            return []
        return [line for line in output.splitlines() if line.strip()]

    def check(self) -> dict[str, Any]:
        """Fetch, then report whether an update is available."""
        self._last_error = None
        try:
            self.fetch()
        except UpdateError as exc:
            self._last_error = str(exc)
        return self.status()

    def status(self) -> dict[str, Any]:
        local = self.current_commit()
        remote = self.remote_commit()
        pending = self.pending_commits() if local and remote and local != remote else []
        return {
            "enabled": self.enabled,
            "branch": self.branch,
            "remote": self.remote,
            "local_commit": local,
            "local_commit_short": local[:8] if local else None,
            "local_describe": self.current_describe(),
            "remote_commit": remote,
            "remote_commit_short": remote[:8] if remote else None,
            "update_available": bool(local and remote and local != remote and pending),
            "pending_commits": pending,
            "dirty": self.is_dirty(),
            "last_fetch": self._last_fetch,
            "last_error": self._last_error,
            "applying": self._applying_since is not None,
            "applying_since": self._applying_since,
        }

    # -- applying ------------------------------------------------------------

    def apply(self) -> dict[str, Any]:
        """Launch the detached updater. Returns immediately; the service restarts."""
        if not self.enabled:
            raise UpdateError("updates are disabled in config")
        script = self.repo_root / "scripts" / "update.sh"
        if not script.exists():
            raise UpdateError(f"{script} is missing")

        self._applying_since = time.time()
        # Prefer the systemd unit: it survives this process being restarted by
        # the very script it launched. Fall back to a bare detached process on
        # a dev box with no systemd.
        if shutil.which("systemctl") and _unit_exists("viabot-update.service"):
            command = ["sudo", "-n", "systemctl", "start", "--no-block",
                       "viabot-update.service"]
        else:
            command = [str(script)]
        try:
            subprocess.Popen(command, cwd=self.repo_root, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self._applying_since = None
            raise UpdateError(f"could not start updater: {exc}") from exc
        return {"started": True, "command": " ".join(command)}


def _unit_exists(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "cat", unit], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
