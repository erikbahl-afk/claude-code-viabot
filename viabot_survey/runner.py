"""Orchestrates the workers and owns survey-run state.

Design note: the measurement workers run for the whole life of the service, not
just during a run. That way the dashboard shows a live link quality the moment
you connect your phone, so you can confirm the rig is healthy *before* walking
into the garage. Starting a run only begins recording (samples to SQLite, video
to disk); stopping it ends recording and leaves the live readout going.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import sysinfo
from .config import Config, redact
from .router_client import build_client
from .storage import Storage
from .workers import CameraWorker, DnsWorker, Iperf3Worker, PingWorker, RouterWorker

log = logging.getLogger(__name__)

STATUS_GOOD = "good"
STATUS_DEGRADED = "degraded"
STATUS_BAD = "bad"
STATUS_DEAD = "dead"
STATUS_UNKNOWN = "unknown"

#: Worst-to-best, used when summarising a stretch of samples.
STATUS_ORDER = (STATUS_DEAD, STATUS_BAD, STATUS_DEGRADED, STATUS_GOOD, STATUS_UNKNOWN)

SAMPLE_INTERVAL_S = 1.0
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def classify(loss_pct: float | None, rtt_ms: float | None, thresholds: dict,
             dead_streak_s: float = 0.0) -> str:
    """Map a ping reading onto the colour the operator sees while walking."""
    if loss_pct is None and rtt_ms is None:
        return STATUS_UNKNOWN
    if loss_pct is not None and loss_pct >= 100:
        if dead_streak_s >= float(thresholds.get("dead_after_s", 5)):
            return STATUS_DEAD
        return STATUS_BAD
    if loss_pct is not None and loss_pct >= float(thresholds.get("bad_loss_pct", 20)):
        return STATUS_BAD
    if rtt_ms is not None and rtt_ms >= float(thresholds.get("bad_rtt_ms", 500)):
        return STATUS_BAD
    if loss_pct is not None and loss_pct >= float(thresholds.get("degraded_loss_pct", 5)):
        return STATUS_DEGRADED
    if rtt_ms is not None and rtt_ms >= float(thresholds.get("degraded_rtt_ms", 200)):
        return STATUS_DEGRADED
    return STATUS_GOOD


def slugify(text: str, limit: int = 32) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")[:limit]


class SurveyRunner:
    def __init__(self, config: Config, storage: Storage) -> None:
        self.config = config
        self.storage = storage
        self._lock = threading.RLock()
        self._run: dict | None = None
        self._sampler: threading.Thread | None = None
        self._stop = threading.Event()
        self._dead_streak_s = 0.0
        self._last_sample: dict | None = None
        self._status_seconds: dict[str, float] = {}
        self._undervoltage_warned = False

        uplink = config["uplink"]
        self.ping = PingWorker(
            target=config["ping"]["target"],
            interval_s=config["ping"]["interval_s"],
            timeout_s=config["ping"]["timeout_s"],
            window_s=config["ping"]["window_s"],
            interface=uplink.get("interface"),
            enabled=config["ping"]["enabled"],
            on_event=self._worker_event("ping"),
        )
        self.dns = DnsWorker(
            hostname=config["dns"]["hostname"],
            interval_s=config["dns"]["interval_s"],
            enabled=config["dns"]["enabled"],
            on_event=self._worker_event("dns"),
        )
        self.iperf = Iperf3Worker(
            server=config["iperf3"]["server"],
            port=config["iperf3"]["port"],
            duration_s=config["iperf3"]["duration_s"],
            interval_s=config["iperf3"]["interval_s"],
            bitrate=config["iperf3"]["bitrate"],
            direction=config["iperf3"]["direction"],
            streams=config["iperf3"]["streams"],
            run_data_budget_mb=config["iperf3"]["run_data_budget_mb"],
            enabled=config["iperf3"]["enabled"],
            on_result=self._on_throughput,
            on_event=self._worker_event("iperf3"),
        )
        self.router = RouterWorker(
            client=build_client(config["router"], uplink["router_address"]),
            interval_s=config["router"]["interval_s"],
            on_event=self._worker_event("router"),
        )
        self.camera = CameraWorker(
            device=config["camera"]["device"],
            width=config["camera"]["width"],
            height=config["camera"]["height"],
            fps=config["camera"]["fps"],
            mode=config["camera"]["mode"],
            segment_s=config["camera"]["segment_s"],
            min_free_disk_mb=config["camera"]["min_free_disk_mb"],
            enabled=config["camera"]["enabled"],
            on_event=self._worker_event("camera"),
        )
        self.workers = [self.ping, self.dns, self.iperf, self.router, self.camera]

    # -- events --------------------------------------------------------------

    def _worker_event(self, source: str):
        def handler(level: str, message: str) -> None:
            run_id = self._run["id"] if self._run else None
            log.log(logging.ERROR if level == "error" else logging.INFO,
                    "[%s] %s", source, message)
            try:
                self.storage.add_event(message, level=level, source=source, run_id=run_id)
            except Exception:  # noqa: BLE001 - never let logging break a worker
                log.exception("failed to persist event")
        return handler

    def _on_throughput(self, result: dict) -> None:
        with self._lock:
            run = self._run
        if not run:
            return
        self.storage.add_throughput(
            run["id"], result["ts"],
            down_mbps=result.get("down_mbps"), up_mbps=result.get("up_mbps"),
            retransmits=result.get("retransmits"), bytes_used=result.get("bytes_used"),
            server=result.get("server"), error=result.get("error"))

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the always-on workers and the sampling loop."""
        self._stop.clear()
        for worker in self.workers:
            if worker is self.camera:
                continue  # the camera only records during a run
            worker.start()
        self._sampler = threading.Thread(target=self._sample_loop, name="sampler",
                                         daemon=True)
        self._sampler.start()
        self.storage.add_event("survey service started", source="runner")

        # Recover from a power cut mid-walk: an unfinished run in the database
        # is closed out rather than silently reopened, so its sample timeline
        # never contains a gap that looks like a dead zone.
        stale = self.storage.active_run()
        if stale:
            self.storage.end_run(stale["id"])
            self.storage.add_event(
                f"closed run {stale['id']} left open by an unclean shutdown",
                level="warning", source="runner", run_id=stale["id"])

    def shutdown(self) -> None:
        self._stop.set()
        if self._run:
            try:
                self.stop_run()
            except Exception:  # noqa: BLE001
                log.exception("error stopping run during shutdown")
        for worker in self.workers:
            worker.stop()
        if self._sampler and self._sampler.is_alive():
            self._sampler.join(timeout=5)
        self.storage.add_event("survey service stopped", source="runner")

    # -- runs ----------------------------------------------------------------

    def start_run(self, label: str = "", git_commit: str | None = None) -> dict:
        with self._lock:
            if self._run:
                raise RuntimeError("a run is already in progress")
            run_id = time.strftime("%Y%m%d-%H%M%S")
            slug = slugify(label)
            if slug:
                run_id = f"{run_id}-{slug}"

            video_dir = self.config.video_dir / run_id
            # Redacted: the run record is handed straight back over the API,
            # and the database is collected onto laptops.
            tz = sysinfo.timezone_info()
            self.storage.create_run(run_id, label=label,
                                    config=redact(self.config.as_dict()),
                                    git_commit=git_commit,
                                    tz_name=tz.get("name"),
                                    tz_offset_s=tz.get("utc_offset_s"))
            self._run = {"id": run_id, "started_at": time.time(), "label": label,
                         "video_dir": video_dir}
            self._dead_streak_s = 0.0
            self._status_seconds = {}

            if not sysinfo.clock_synced():
                # Worth shouting about: without a disciplined clock the video
                # timestamps and the samples can disagree, which defeats the
                # entire purpose of the rig.
                self.storage.add_event(
                    "system clock is NOT NTP-synchronised; video/sample correlation "
                    "may be off", level="warning", source="runner", run_id=run_id)

            self.iperf.reset_run_budget()
            if self.camera.enabled:
                self.camera.set_output_dir(video_dir)
                self.camera.start()

            self.storage.add_event(f"run {run_id} started", source="runner", run_id=run_id)
            return self.storage.get_run(run_id)

    def stop_run(self) -> dict | None:
        with self._lock:
            run = self._run
            if not run:
                return None
            self._run = None

        self.camera.stop()
        self.camera.output_dir = None
        self.storage.end_run(run["id"])
        self.storage.add_event(f"run {run['id']} stopped", source="runner",
                               run_id=run["id"])
        return self.storage.get_run(run["id"])

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._run["id"] if self._run else None

    def add_mark(self, category: str = "", note: str = "",
                 ts: float | None = None) -> dict:
        with self._lock:
            run = self._run
        if not run:
            raise RuntimeError("no run in progress")
        ts = time.time() if ts is None else ts
        video_file, offset = self.camera.locate(ts)
        status = (self._last_sample or {}).get("status")
        mark_id = self.storage.add_mark(run["id"], ts, category=category, note=note,
                                        status=status, video_file=video_file,
                                        video_offset_s=offset)
        return {"id": mark_id, "run_id": run["id"], "ts": ts, "category": category,
                "note": note, "status": status, "video_file": video_file,
                "video_offset_s": offset}

    # -- sampling ------------------------------------------------------------

    def _sample_loop(self) -> None:
        # Schedule against the monotonic clock so sampling stays on a 1 Hz grid
        # even when writing a sample takes a moment.
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += SAMPLE_INTERVAL_S
            try:
                self.collect_sample()
            except Exception:  # noqa: BLE001 - a bad sample must not stop sampling
                log.exception("sampling failed")
            delay = next_tick - time.monotonic()
            if delay < 0:
                next_tick = time.monotonic()
                delay = 0
            if self._stop.wait(delay):
                return

    def collect_sample(self) -> dict:
        now = time.time()
        ping = self.ping.snapshot(now) if self.ping.enabled else {}
        loss = ping.get("loss_pct")
        rtt = ping.get("rtt_ms")

        if loss is not None and loss >= 100:
            self._dead_streak_s += SAMPLE_INTERVAL_S
        else:
            self._dead_streak_s = 0.0

        status = classify(loss, rtt, self.config["thresholds"], self._dead_streak_s)
        dns = self.dns.snapshot() if self.dns.enabled else {}
        signal = self.router.sample_fields() if self.router.enabled else {}
        undervoltage = self._check_power()

        sample: dict[str, Any] = {
            "ts": now,
            "rtt_ms": rtt,
            "loss_pct": loss,
            "jitter_ms": ping.get("jitter_ms"),
            "status": status,
            "dns_ms": dns.get("resolve_ms"),
            "dead_streak_s": round(self._dead_streak_s, 1),
            "undervoltage": undervoltage,
            **signal,
        }
        self._last_sample = sample

        with self._lock:
            run = self._run
        if run:
            video_file, offset = self.camera.locate(now)
            self._status_seconds[status] = self._status_seconds.get(status, 0.0) + SAMPLE_INTERVAL_S
            self.storage.add_sample(
                run["id"], now,
                rtt_ms=rtt, loss_pct=loss, jitter_ms=ping.get("jitter_ms"),
                status=status, dns_ms=dns.get("resolve_ms"),
                video_file=video_file, video_offset_s=offset,
                clock_synced=1 if sysinfo.clock_synced() else 0,
                undervoltage=undervoltage,
                **signal)
        return sample

    def _check_power(self) -> int | None:
        """Flag a sagging supply, once, loudly.

        A brownout on the rig's screw-terminal splice degrades measurements in
        ways that read as bad coverage, so it has to be distinguishable in the
        data rather than left to be discovered afterwards.
        """
        power = sysinfo.power_health()
        if power is None:
            return None
        live = bool(power.get("undervoltage_now") or power.get("throttled_now"))
        if live and not self._undervoltage_warned:
            self._undervoltage_warned = True
            self.storage.add_event(
                "UNDERVOLTAGE: the Pi's supply is sagging. Check the screw-terminal "
                "splice and the USB-C cable — readings taken now are unreliable.",
                level="error", source="power", run_id=self.active_run_id)
        elif not live:
            self._undervoltage_warned = False
        return 1 if live else 0

    # -- reporting -----------------------------------------------------------

    def run_summary(self) -> dict | None:
        with self._lock:
            run = self._run
        if not run:
            return None
        duration = time.time() - run["started_at"]
        return {
            "id": run["id"],
            "label": run["label"],
            "started_at": run["started_at"],
            "duration_s": round(duration, 1),
            "video_dir": str(run["video_dir"]),
            "mark_count": len(self.storage.list_marks(run["id"])),
            "status_seconds": {k: round(v, 1) for k, v in self._status_seconds.items()},
            "dead_seconds": round(self._status_seconds.get(STATUS_DEAD, 0.0), 1),
            "data_used_mb": round(self.storage.run_data_used_bytes(run["id"]) / 1e6, 1),
        }

    def status(self) -> dict:
        return {
            "now": time.time(),
            "sample": self._last_sample or {"status": STATUS_UNKNOWN},
            "run": self.run_summary(),
            "workers": {worker.name: worker.status() for worker in self.workers},
            "iperf_budget_remaining_mb": self.iperf.budget_remaining_mb,
        }

    def history(self, seconds: float = 180) -> list[dict]:
        """Recent samples for the sparkline. Falls back to the live reading when
        no run is recording, so the chart still moves before you press Start."""
        run_id = self.active_run_id
        if not run_id:
            return [self._last_sample] if self._last_sample else []
        rows = self.storage.recent_samples(run_id, seconds)
        return [{"ts": r["ts"], "rtt_ms": r["rtt_ms"], "loss_pct": r["loss_pct"],
                 "status": r["status"]} for r in rows]

    def sysinfo(self) -> dict:
        return sysinfo.collect(
            uplink_interface=self.config["uplink"]["interface"],
            ap_interface=self.config["ap"]["interface"],
            data_dir=Path(self.config.data_dir))
