"""Send finished runs to the receiving server, a chunk at a time.

Two rules shape this worker, and both come from what the rig is.

**It never uploads during a walk.** The upload would travel over the cellular
link the survey is measuring, and saturating that link would manufacture the
latency and loss the run is supposed to be recording. A run starting mid-upload
interrupts it between chunks; the queue remembers, and it resumes afterwards.

**It assumes it will be interrupted.** Every chunk that lands is recorded
before the next is attempted, so the worst a power cut costs is one chunk.
Progress lives in the database rather than in this object, which is why pulling
the plug and booting again picks up where the walk left off.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..publish import PublishClient, PublishError
from .base import STATE_DEGRADED, STATE_RUNNING, Worker


class PublisherWorker(Worker):
    name = "publisher"

    #: Consecutive failed cycles before the dashboard calls it degraded. A rig
    #: in a basement has no uplink at all, and that is not a fault.
    FAILURE_GRACE = 3

    def __init__(self, client: PublishClient, storage: Any,
                 busy: Callable[[], bool] | None = None,
                 report_for: Callable[[str], tuple[Path, dict]] | None = None,
                 assemble_video: Callable[[str, Path], None] | None = None,
                 interval_s: float = 60.0, enabled: bool = True,
                 **kwargs: Any) -> None:
        super().__init__(enabled=enabled, **kwargs)
        self.client = client
        self.storage = storage
        self.busy = busy or (lambda: False)
        self.report_for = report_for
        self.assemble_video = assemble_video
        self.interval_s = max(10.0, float(interval_s))
        self._sent_bytes = 0
        self._last_ok_ts: float | None = None
        self._last_error: str | None = None
        self._failures = 0
        self._manifested: set[str] = set()

    # -- loop ----------------------------------------------------------------

    def run_once(self) -> None:
        while not self.stopping:
            if not self.busy():
                self.cycle()
            if not self.wait(self.interval_s):
                return

    def cycle(self) -> None:
        """One pass: collect what was asked for, then send what is owed."""
        try:
            self.collect_requests()
            sent = self.drain()
        except PublishError as exc:
            self._note_failure(str(exc))
            return
        except OSError as exc:
            self._note_failure(f"{exc}")
            return
        self._failures = 0
        self._last_error = None
        self._set_state(STATE_RUNNING)
        if sent:
            self._last_ok_ts = time.time()

    def _note_failure(self, detail: str) -> None:
        self._failures += 1
        self._last_error = detail[:300]
        if self._failures >= self.FAILURE_GRACE:
            self._set_state(STATE_DEGRADED, self._last_error)

    # -- work ----------------------------------------------------------------

    def drain(self) -> int:
        """Upload every pending file. Returns how many finished."""
        finished = 0
        for row in self.storage.pending_uploads():
            if self.stopping or self.busy():
                break
            if self.send_one(row):
                finished += 1
        return finished

    def send_one(self, row: dict) -> bool:
        run_id, name = row["run_id"], row["remote_name"]
        path = Path(row["local_path"])

        if not path.exists():
            # The only file expected to be missing is the full walk video,
            # which is assembled from segments the first time it is wanted —
            # there is no reason to hold a second copy of a whole survey on a
            # card that also has to hold the next one.
            if row["kind"] == "video" and self.assemble_video:
                try:
                    self.assemble_video(run_id, path)
                except Exception as exc:  # noqa: BLE001
                    self.storage.update_upload(row["id"], state="failed",
                                               error=f"could not assemble: {exc}",
                                               bump_attempts=True)
                    return False
            if not path.exists():
                self.storage.update_upload(
                    row["id"], state="failed", error="file is gone",
                    bump_attempts=True)
                self.emit("warning", f"{name} for {run_id} is gone; not publishing")
                return False

        self.ensure_manifest(run_id)
        try:
            sent = self.client.upload(
                run_id, name, path,
                on_progress=lambda offset: self.storage.update_upload(
                    row["id"], sent_bytes=offset),
                should_stop=lambda: self.stopping or self.busy())
        except PublishError as exc:
            self.storage.update_upload(row["id"], error=str(exc), bump_attempts=True)
            raise

        total = path.stat().st_size
        self._sent_bytes += max(0, sent - (row["sent_bytes"] or 0))
        if sent >= total:
            self.storage.update_upload(row["id"], sent_bytes=sent, state="done")
            self.emit("info", f"published {name} for {run_id}")
            return True
        # Stopped part way, deliberately. The row stays pending with its
        # progress recorded, which is the whole point of the queue.
        self.storage.update_upload(row["id"], sent_bytes=sent)
        return False

    def ensure_manifest(self, run_id: str) -> None:
        """Tell the receiver what this run is, once per process."""
        if run_id in self._manifested or not self.report_for:
            return
        run = self.storage.get_run(run_id)
        if not run:
            return
        summary = self.storage.list_dead_zones(run_id)
        self.client.put_manifest(run_id, {
            "label": run.get("label"),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "runnable_pct": run.get("runnable_pct"),
            "dead_zone_count": len(summary),
        })
        self._manifested.add(run_id)

    def collect_requests(self) -> None:
        """Ask the receiver whether anyone pressed 'request the full video'."""
        for run_id in self.storage.runs_with_held_uploads():
            if self.stopping or self.busy():
                return
            wanted = self.client.requests_for(run_id)
            if wanted.get("full_video"):
                released = self.storage.release_held_uploads(run_id, "video")
                if released:
                    self.emit("info", f"full video requested for {run_id}")

    # -- reporting -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        summary = self.storage.upload_summary()
        return {
            "target": self.client.base_url,
            "sent_bytes": self._sent_bytes,
            "last_ok_ts": self._last_ok_ts,
            "last_error": self._last_error,
            **summary,
        }
