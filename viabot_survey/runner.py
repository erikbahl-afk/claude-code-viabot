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

from . import deadzones, report, sysinfo
from .config import Config, redact
from .publish import PublishClient
from .router_client import build_client
from .storage import Storage
from .workers import CameraWorker, DnsWorker, Iperf3Worker, PingWorker, RouterWorker
from .workers.publisher import PublisherWorker

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
        self._paused = False
        self._wrapup: dict | None = None
        # Counted live so the operator sees zones accumulate while walking.
        # The authoritative figure is recomputed from stored samples at the end.
        self._dead_zone_estimate = 0
        self._in_dead_zone = False

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
            capture_fps=config["camera"].get("capture_fps"),
            mode=config["camera"]["mode"],
            segment_s=config["camera"]["segment_s"],
            min_free_disk_mb=config["camera"]["min_free_disk_mb"],
            enabled=config["camera"]["enabled"],
            on_event=self._worker_event("camera"),
        )
        publish = config["publish"]
        self.publisher = PublisherWorker(
            client=PublishClient(
                base_url=publish["url"],
                token=publish["token"],
                chunk_bytes=publish["chunk_bytes"],
                timeout=publish["timeout_s"],
            ),
            storage=storage,
            # A survey must never compete with its own upload for the link it
            # is measuring, so the queue idles for the length of a walk.
            busy=lambda: self.active_run_id is not None,
            report_for=self._report_bundle,
            assemble_video=self.assemble_full_video,
            interval_s=publish["interval_s"],
            enabled=bool(publish["enabled"]) and bool(publish["url"]),
            on_event=self._worker_event("publisher"),
        )
        self.workers = [self.ping, self.dns, self.iperf, self.router, self.camera,
                        self.publisher]

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

        self._recover_interrupted_run()

    def _recover_interrupted_run(self) -> None:
        """Salvage a walk the rig was cut off in the middle of.

        An unfinished run in the database means power was lost mid-walk. The
        run is closed rather than silently reopened, so its sample timeline
        never contains a gap that would read as a dead zone — but everything up
        to the interruption is perfectly good data, so it is also analysed.
        Without that, a power cut costs the whole walk instead of its last few
        seconds, and someone has to drive back to the garage.

        Runs on a background thread: the analysis cuts video clips, and the
        dashboard should not be unreachable while that happens.
        """
        stale = self.storage.active_run()
        if not stale:
            return
        run_id = stale["id"]
        self.storage.end_run(run_id)
        self.storage.add_event(
            f"run {run_id} was cut off mid-walk; analysing what was recorded",
            level="warning", source="runner", run_id=run_id)

        def salvage() -> None:
            try:
                result = self.analyse_run(run_id)
                summary = result["summary"]
                if summary.get("runnable_pct") is None:
                    self.storage.add_event(
                        f"run {run_id} had no usable samples to analyse",
                        level="warning", source="runner", run_id=run_id)
            except Exception:  # noqa: BLE001 - salvage must never block startup
                log.exception("could not salvage %s", run_id)
                self.storage.add_event(
                    f"could not analyse the interrupted run {run_id}; its samples "
                    "and video are still on disk",
                    level="error", source="runner", run_id=run_id)

        threading.Thread(target=salvage, name="salvage", daemon=True).start()

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
        # A run without a location name is just a timestamp, and a week later
        # nobody knows which garage it was.
        slug = slugify(label)
        if not slug:
            raise ValueError("a location name is required to start a run")
        with self._lock:
            if self._run:
                raise RuntimeError("a run is already in progress")
            run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}"

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
            self._paused = False
            self._wrapup = None
            self._dead_zone_estimate = 0
            self._in_dead_zone = False

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
        """End the run, then work out where the dead zones were."""
        with self._lock:
            run = self._run
            if not run:
                return None
            self._run = None
            self._paused = False

        segments = self.camera.segments()
        self.camera.stop()
        self.camera.output_dir = None
        self.storage.end_run(run["id"])
        self.storage.add_event(f"run {run['id']} stopped", source="runner",
                               run_id=run["id"])

        try:
            self.analyse_run(run["id"], run["video_dir"], segments)
        except Exception:  # noqa: BLE001 - a failed analysis must not lose the run
            log.exception("analysis failed for %s", run["id"])
            self.storage.add_event(
                "could not analyse the run; the raw samples and video are still "
                "on disk and it can be re-analysed",
                level="error", source="runner", run_id=run["id"])
        return self.storage.get_run(run["id"])

    def analyse_run(self, run_id: str, video_dir: Path | None = None,
                    segments: list | None = None) -> dict:
        """Find the dead zones, cut their clips, and store the summary.

        Split out from :meth:`stop_run` so a finished run can be re-analysed
        after the thresholds change, without re-walking the garage.
        """
        config = self.config["deadzone"]
        samples = list(self.storage.iter_samples(run_id))
        zones = deadzones.find_dead_zones(samples, config)
        summary = deadzones.summarise(samples, zones)

        if video_dir is None:
            video_dir = self.config.video_dir / run_id
        video_dir = Path(video_dir)
        if segments is None:
            segments = _segments_on_disk(video_dir)

        if zones and config.get("extract_clips", True) and segments:
            clip_dir = Path(self.config.data_dir) / "clips" / run_id
            # The effective mode, not the configured one: an overlay that had
            # to fall back leaves raw MJPEG, which an .mp4 will not hold.
            container = "mp4" if self.camera.effective_mode == "overlay" else "mkv"
            self.storage.add_event(
                f"cutting {len(zones)} dead-zone clip(s)", source="runner",
                run_id=run_id)
            for zone in zones:
                deadzones.extract_clip(
                    zone, segments, video_dir, clip_dir, config,
                    segment_length_s=float(self.config["camera"]["segment_s"]),
                    container=container)
                if zone.clip_error:
                    self.storage.add_event(
                        f"dead zone {zone.index}: no clip ({zone.clip_error})",
                        level="warning", source="runner", run_id=run_id)
            summary["clip_dir"] = str(clip_dir)

        summary["thresholds_provisional"] = bool(config.get("provisional", False))
        self.storage.replace_dead_zones(run_id, [z.as_dict() for z in zones])
        self.storage.set_run_summary(run_id, summary)

        message = (f"run {run_id}: {summary['runnable_pct']}% runnable, "
                   f"{summary['dead_zone_count']} dead zone(s)"
                   if summary["runnable_pct"] is not None
                   else f"run {run_id}: no samples recorded")
        self.storage.add_event(message, source="runner", run_id=run_id)
        self._wrapup = {"run_id": run_id, "summary": summary,
                        "zones": [z.as_dict() for z in zones]}
        try:
            self.queue_for_publishing(run_id)
        except Exception:  # noqa: BLE001 - publishing must never lose a result
            log.exception("could not queue %s for publishing", run_id)
            self.storage.add_event(
                "the run was analysed but could not be queued for upload; "
                "its report and clips are still on the rig",
                level="warning", source="publisher", run_id=run_id)
        return self._wrapup

    # -- publishing ----------------------------------------------------------

    def _report_bundle(self, run_id: str) -> tuple[Path, dict]:
        """Where a run's publishable files are built, and the report itself."""
        run = self.storage.get_run(run_id)
        built = report.build_report(self.storage, run) if run else {}
        return Path(self.config.data_dir) / "reports" / run_id, built

    def queue_for_publishing(self, run_id: str) -> int:
        """Write the report and hand the whole bundle to the upload queue.

        Queued, not uploaded: the rig may be underground with no usable link,
        or about to be switched off. Everything here is durable, so whenever
        the rig next has power and is not walking, it carries on.
        """
        if not self.config["publish"]["enabled"]:
            return 0
        bundle_dir, built = self._report_bundle(run_id)
        if not built:
            return 0
        bundle_dir.mkdir(parents=True, exist_ok=True)

        page = bundle_dir / "index.html"
        page.write_text(report.render_html(
            built, deadzone_config=self.config["deadzone"]), encoding="utf-8")
        self.storage.enqueue_upload(run_id, "report", str(page), "index.html",
                                    page.stat().st_size)
        queued = 1

        for zone in built.get("dead_zones") or []:
            clip = zone.get("clip_path")
            if not clip or not Path(clip).exists():
                continue
            name = Path(clip).name
            self.storage.enqueue_upload(run_id, "clip", clip, f"clips/{name}",
                                        Path(clip).stat().st_size)
            queued += 1

        # The whole walk is several hundred megabytes against a few tens for
        # the clips, over a metered link, so it waits to be asked for. It is
        # not even assembled until then.
        if self.config["publish"]["offer_full_video"]:
            self.storage.enqueue_upload(
                run_id, "video", str(bundle_dir / "full.mp4"), "video/full.mp4",
                None, held=True)

        self.storage.add_event(f"queued {queued} file(s) for upload",
                               source="publisher", run_id=run_id)
        return queued

    def assemble_full_video(self, run_id: str, out_path: Path) -> None:
        """Join the run's segments into one file, without re-encoding.

        Built only when somebody asks for it. Stream-copied, so it costs disk
        and a little I/O rather than an hour of the Pi's CPU.
        """
        video_dir = self.config.video_dir / run_id
        segments = _segments_on_disk(video_dir)
        if not segments:
            raise FileNotFoundError(f"no video segments for {run_id}")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        listing = out_path.with_suffix(".txt")
        listing.write_text("".join(
            f"file '{(video_dir / name).resolve()}'\n" for name, _ in segments))
        try:
            deadzones._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing),
                               "-c", "copy", str(out_path)], timeout=1800)
        finally:
            listing.unlink(missing_ok=True)
        if not out_path.exists() or out_path.stat().st_size == 0:
            out_path.unlink(missing_ok=True)
            raise RuntimeError("ffmpeg produced no video")
        self.storage.enqueue_upload(run_id, "video", str(out_path),
                                    "video/full.mp4", out_path.stat().st_size)

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._run["id"] if self._run else None

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def pause(self) -> bool:
        """Stop recording and measuring without ending the run.

        Paused time leaves no samples and no video at all, which is the point:
        the headline result is a percentage of time walked, so standing still
        in a good spot must not count as coverage. The gap this leaves in the
        timeline is recognised by the dead-zone detector, which never stitches
        a zone across it.
        """
        with self._lock:
            if not self._run or self._paused:
                return False
            self._paused = True
            run_id = self._run["id"]
        self.camera.stop()
        self._dead_streak_s = 0.0
        self.storage.add_event("run paused", source="runner", run_id=run_id)
        return True

    def resume(self) -> bool:
        with self._lock:
            if not self._run or not self._paused:
                return False
            self._paused = False
            run_id = self._run["id"]
            video_dir = self._run["video_dir"]
        if self.camera.enabled:
            # A fresh ffmpeg writes a new segment named for the current wall
            # clock, so correlation still holds across the gap.
            self.camera.set_output_dir(video_dir)
            self.camera.start()
        self.storage.add_event("run resumed", source="runner", run_id=run_id)
        return True

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

        self._track_dead_zone({"loss_pct": loss, "rtt_ms": rtt})

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
            paused = self._paused
        if run and not paused:
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

    def _track_dead_zone(self, sample: dict) -> None:
        """Count dead zones as they happen, using the same rule as the report."""
        config = self.config["deadzone"]
        unusable = deadzones.is_unusable(sample, config)
        if unusable and not self._in_dead_zone:
            if self._dead_streak_s >= float(config.get("min_duration_s", 5)):
                self._in_dead_zone = True
                self._dead_zone_estimate += 1
        elif not unusable:
            self._in_dead_zone = False

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
        # Walked time is counted from samples, not the wall clock, so a pause
        # genuinely does not exist in the numbers.
        walked_s = sum(self._status_seconds.values())
        return {
            "id": run["id"],
            "label": run["label"],
            "started_at": run["started_at"],
            "paused": self._paused,
            "walked_s": round(walked_s, 1),
            "elapsed_s": round(time.time() - run["started_at"], 1),
            "video_dir": str(run["video_dir"]),
            "status_seconds": {k: round(v, 1) for k, v in self._status_seconds.items()},
            "dead_seconds": round(self._status_seconds.get(STATUS_DEAD, 0.0), 1),
            "dead_zone_estimate": self._dead_zone_estimate,
            "data_used_mb": round(self.storage.run_data_used_bytes(run["id"]) / 1e6, 1),
        }

    def status(self) -> dict:
        return {
            "now": time.time(),
            "sample": self._last_sample or {"status": STATUS_UNKNOWN},
            "run": self.run_summary(),
            "paused": self.paused,
            "health": self.health(),
            "wrapup": self._wrapup,
            "workers": {worker.name: worker.status() for worker in self.workers},
            "iperf_budget_remaining_mb": self.iperf.budget_remaining_mb,
        }

    def clear_wrapup(self) -> None:
        self._wrapup = None

    def health(self) -> dict:
        """The handful of things that must be visible without opening anything.

        A rig whose camera has quietly died is still cheerfully reporting
        connection quality, and the whole walk is wasted — so camera state is
        first, and failures say what to do rather than only that something is
        wrong.
        """
        run_active = self.active_run_id is not None and not self.paused
        camera = self.camera
        if not camera.enabled:
            camera_state, camera_detail = "off", "disabled in config"
        elif camera.state == "failed":
            camera_state = "fail"
            camera_detail = camera.error or "recording stopped"
        elif run_active and not camera.snapshot().get("recording"):
            camera_state, camera_detail = "fail", "not recording"
        elif run_active:
            camera_state = "ok"
            camera_detail = ("recording" if camera.effective_mode == "overlay"
                             else "recording (no clock overlay)")
        else:
            camera_state, camera_detail = "idle", "ready"

        ping = self.ping
        if not ping.enabled:
            link_state, link_detail = "off", "disabled in config"
        elif ping.state in ("failed", "degraded"):
            link_state = "fail"
            link_detail = ping.error or "no replies"
        else:
            link_state, link_detail = "ok", "measuring"

        power = sysinfo.power_health()
        if power is None:
            power_state, power_detail = "unknown", "not readable"
        elif power.get("undervoltage_now") or power.get("throttled_now"):
            power_state, power_detail = "fail", "supply sagging — check the splice"
        elif power.get("undervoltage_since_boot"):
            power_state, power_detail = "warn", "dipped earlier"
        else:
            power_state, power_detail = "ok", "steady"

        free_mb = camera.snapshot().get("free_disk_mb")
        if free_mb is None:
            disk = sysinfo.disk_usage(Path(self.config.data_dir))
            free_mb = disk["free_mb"]
        floor = float(self.config["camera"]["min_free_disk_mb"])
        if free_mb < floor:
            disk_state, disk_detail = "fail", f"{free_mb / 1000:.1f} GB left"
        elif free_mb < floor * 3:
            disk_state, disk_detail = "warn", f"{free_mb / 1000:.1f} GB left"
        else:
            disk_state, disk_detail = "ok", f"{free_mb / 1000:.0f} GB free"

        synced = sysinfo.clock_synced()
        if synced is None:
            clock_state, clock_detail = "unknown", "cannot tell"
        elif synced:
            clock_state, clock_detail = "ok", "synced"
        else:
            clock_state, clock_detail = "fail", "NOT synced — timestamps unreliable"

        return {
            "camera": {"state": camera_state, "detail": camera_detail},
            "link": {"state": link_state, "detail": link_detail},
            "power": {"state": power_state, "detail": power_detail},
            "disk": {"state": disk_state, "detail": disk_detail},
            "clock": {"state": clock_state, "detail": clock_detail},
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


def _segments_on_disk(video_dir: Path) -> list[tuple[str, float]]:
    """Segment (name, start epoch) pairs for a run whose camera worker is gone.

    Re-analysing a finished run has no live CameraWorker to ask, so the segment
    list is rebuilt from the UTC filenames on disk.
    """
    from .workers.camera import segment_start_epoch

    if not video_dir.exists():
        return []
    found = []
    for path in video_dir.iterdir():
        start = segment_start_epoch(path.name)
        if start is not None:
            found.append((path.name, start))
    found.sort(key=lambda item: item[1])
    return found
