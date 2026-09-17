"""Flask app: captive portal + operator dashboard + JSON API.

The captive portal is the trick that makes this usable one-handed: phones probe
a handful of well-known URLs to decide whether a Wi-Fi network has internet, and
our dnsmasq answers *every* DNS name with the Pi's address. When a probe lands
here we answer with a redirect instead of the expected magic response, so the
phone concludes it is behind a captive portal and pops the dashboard open
automatically — no typing an IP address in a dark garage.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
import time
from typing import Any

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, stream_with_context)

from . import __version__
from .config import Config, redact
from .runner import SurveyRunner
from .report import build_report, iso_local, iso_utc, render_html
from .storage import Storage
from .updater import UpdateError, Updater

log = logging.getLogger(__name__)

# Paths phones and laptops hit to test for internet access. Answering any of
# these with something other than the expected response triggers the portal.
CAPTIVE_PROBE_PATHS = (
    "/generate_204",            # Android
    "/gen_204",                 # Android (older)
    "/hotspot-detect.html",     # iOS / macOS
    "/library/test/success.html",
    "/connecttest.txt",         # Windows
    "/ncsi.txt",
    "/redirect",
    "/canonical.html",          # Firefox
    "/success.txt",
    "/chat",                    # Some Android builds
)

SYSINFO_TTL_S = 5.0



class _SysinfoCache:
    """sysinfo shells out to ip/timedatectl; at 1 Hz polling from several
    phones that adds up, so serve a short-lived cached copy."""

    def __init__(self, runner: SurveyRunner, ttl: float = SYSINFO_TTL_S) -> None:
        self._runner = runner
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._fetched_at = 0.0

    def get(self) -> dict:
        with self._lock:
            if self._value is not None and time.monotonic() - self._fetched_at < self._ttl:
                return self._value
        value = self._runner.sysinfo()
        with self._lock:
            self._value = value
            self._fetched_at = time.monotonic()
        return value


def create_app(config: Config, runner: SurveyRunner, storage: Storage,
               updater: Updater) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    sysinfo_cache = _SysinfoCache(runner)

    portal_root = f"http://{config['ap']['address']}/"
    allowed_hosts = _build_allowed_hosts(config)

    # ---- captive portal ----------------------------------------------------

    @app.before_request
    def _captive_redirect():
        if not config["web"].get("captive_portal", True):
            return None
        host = (request.host or "").split(":")[0].lower()
        if host in allowed_hosts:
            return None
        # A request for some other hostname only reaches us because DNS is
        # hijacked, i.e. it is a connectivity probe or a stray browser tab.
        return _portal_response(portal_root)

    for path in CAPTIVE_PROBE_PATHS:
        app.add_url_rule(path, f"probe_{path.strip('/').replace('/', '_')}",
                         lambda: _portal_response(portal_root))

    @app.errorhandler(404)
    def _not_found(_error):
        if config["web"].get("captive_portal", True) and not request.path.startswith("/api/"):
            return _portal_response(portal_root)
        return jsonify({"error": "not found", "path": request.path}), 404

    # ---- pages -------------------------------------------------------------

    @app.route("/")
    def index():
        return render_template(
            "dashboard.html",
            version=__version__,
            ap_address=config["ap"]["address"],
        )

    # ---- status ------------------------------------------------------------

    @app.route("/api/status")
    def api_status():
        payload = runner.status()
        payload["system"] = sysinfo_cache.get()
        payload["version"] = __version__
        payload["thresholds_provisional"] = bool(
            config["thresholds"].get("provisional", False))
        return jsonify(payload)

    @app.route("/api/history")
    def api_history():
        seconds = request.args.get("seconds", default=180, type=float)
        return jsonify({"samples": runner.history(min(max(seconds, 10), 3600))})

    @app.route("/api/events")
    def api_events():
        limit = request.args.get("limit", default=40, type=int)
        return jsonify({"events": storage.recent_events(limit=min(max(limit, 1), 500))})

    @app.route("/api/config")
    def api_config():
        return jsonify(redact(config.as_dict()))

    # ---- run control -------------------------------------------------------

    @app.route("/api/run/start", methods=["POST"])
    def api_run_start():
        label = (request.get_json(silent=True) or {}).get("label", "")
        try:
            run = runner.start_run(label=str(label)[:120],
                                   git_commit=updater.current_commit())
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"run": run})

    @app.route("/api/run/pause", methods=["POST"])
    def api_run_pause():
        if not runner.pause():
            return jsonify({"error": "no run in progress, or already paused"}), 409
        return jsonify({"paused": True})

    @app.route("/api/run/resume", methods=["POST"])
    def api_run_resume():
        if not runner.resume():
            return jsonify({"error": "no run in progress, or not paused"}), 409
        return jsonify({"paused": False})

    @app.route("/api/run/stop", methods=["POST"])
    def api_run_stop():
        run = runner.stop_run()
        if run is None:
            return jsonify({"error": "no run in progress"}), 409
        return jsonify({"run": run, "wrapup": runner.status().get("wrapup")})

    @app.route("/api/run/wrapup/dismiss", methods=["POST"])
    def api_dismiss_wrapup():
        runner.clear_wrapup()
        return jsonify({"cleared": True})

    @app.route("/api/runs/<run_id>/analyse", methods=["POST"])
    def api_reanalyse(run_id: str):
        """Re-run detection on a finished run after changing the thresholds."""
        if storage.get_run(run_id) is None:
            return jsonify({"error": "unknown run"}), 404
        if run_id == runner.active_run_id:
            return jsonify({"error": "the run is still in progress"}), 409
        return jsonify(runner.analyse_run(run_id))

    @app.route("/api/iperf/test", methods=["POST"])
    def api_iperf_test():
        if not runner.iperf.enabled:
            return jsonify({
                "error": "iperf3 is disabled. Set iperf3.server and iperf3.enabled "
                         "in config/config.yaml — see docs/IPERF_SERVER.md."
            }), 409
        runner.iperf.request_manual_test()
        return jsonify({"queued": True})

    # ---- runs and export ---------------------------------------------------

    @app.route("/api/runs")
    def api_runs():
        return jsonify({"runs": storage.list_runs(limit=200),
                        "active": runner.active_run_id})

    @app.route("/api/runs/<run_id>", methods=["DELETE"])
    def api_delete_run(run_id: str):
        if run_id == runner.active_run_id:
            return jsonify({"error": "cannot delete the run in progress"}), 409
        if storage.get_run(run_id) is None:
            return jsonify({"error": "unknown run"}), 404
        return jsonify(runner.discard_run(run_id))

    @app.route("/api/runs/<run_id>/samples.csv")
    def api_export_samples(run_id: str):
        if storage.get_run(run_id) is None:
            return jsonify({"error": "unknown run"}), 404
        columns = ["ts", "iso_utc", "iso_local", "status", "rtt_ms", "loss_pct",
                   "jitter_ms", "dns_ms", "rsrp", "rsrq", "sinr", "rssi", "band",
                   "cell_id", "tech", "video_file", "video_offset_s",
                   "clock_synced", "undervoltage"]

        def generate():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(columns)
            yield _drain(buffer)
            for row in storage.iter_samples(run_id):
                row["iso_utc"] = _iso_utc(row["ts"])
                row["iso_local"] = _iso(row["ts"])
                writer.writerow([row.get(name) for name in columns])
                yield _drain(buffer)

        return Response(stream_with_context(generate()), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{run_id}-samples.csv"'})

    @app.route("/api/runs/<run_id>/deadzones.csv")
    def api_export_dead_zones(run_id: str):
        if storage.get_run(run_id) is None:
            return jsonify({"error": "unknown run"}), 404
        columns = ["idx", "start_utc", "start_local", "duration_s",
                   "worst_loss_pct", "worst_rtt_ms", "video_file",
                   "video_offset_s", "clip_path", "clip_error"]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        for zone in storage.list_dead_zones(run_id):
            zone["start_utc"] = _iso_utc(zone["start_ts"])
            zone["start_local"] = _iso(zone["start_ts"])
            writer.writerow([zone.get(name) for name in columns])
        return Response(buffer.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{run_id}-deadzones.csv"'})

    @app.route("/api/runs/<run_id>/report")
    def api_run_report(run_id: str):
        run = storage.get_run(run_id)
        if run is None:
            return jsonify({"error": "unknown run"}), 404
        return jsonify(build_report(storage, run, config["udp_load"]))

    @app.route("/api/runs/<run_id>/report.html")
    def api_run_report_html(run_id: str):
        """The same report as a page — the thing that gets published.

        Served from the rig too, so a report can be read before it has been
        uploaded anywhere, and so a failed upload is never the only copy.
        """
        run = storage.get_run(run_id)
        if run is None:
            return jsonify({"error": "unknown run"}), 404
        page = render_html(build_report(storage, run, config["udp_load"]),
                           deadzone_config=config["deadzone"],
                           version=__version__)
        return Response(page, mimetype="text/html; charset=utf-8")

    @app.route("/api/runs/<run_id>/publish", methods=["POST"])
    def api_run_publish(run_id: str):
        """Queue a finished run for upload.

        Runs analysed before publishing was switched on never got queued, and
        re-cutting clips after a threshold change produces new files worth
        sending. Queuing is idempotent: a file already uploaded stays uploaded,
        and one halfway there keeps its progress.
        """
        if storage.get_run(run_id) is None:
            return jsonify({"error": "unknown run"}), 404
        if not config["publish"]["enabled"]:
            return jsonify({"error": "publishing is disabled in config"}), 409
        queued = runner.queue_for_publishing(run_id)
        return jsonify({"ok": True, "queued": queued,
                        "uploads": storage.list_uploads(run_id)})

    # ---- updates -----------------------------------------------------------

    @app.route("/api/update/status")
    def api_update_status():
        return jsonify(updater.status())

    @app.route("/api/update/check", methods=["POST"])
    def api_update_check():
        return jsonify(updater.check())

    @app.route("/api/update/apply", methods=["POST"])
    def api_update_apply():
        if runner.active_run_id:
            return jsonify({"error": "stop the run before updating"}), 409
        try:
            result = updater.apply()
        except UpdateError as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify(result)

    # When *this* process started answering. An update restarts the service,
    # and the dashboard has no other way to tell "the rig came back" from "the
    # old process never went away" — which matters because update.sh fetches
    # and installs for a while before it restarts anything, and answers health
    # checks perfectly happily the whole time.
    started_at = time.time()

    @app.route("/api/health")
    def api_health():
        return jsonify({"ok": True, "version": __version__,
                        "started_at": started_at,
                        "run": runner.active_run_id})

    return app


def _portal_response(portal_root: str) -> Response:
    """Redirect a connectivity probe at the dashboard.

    302 with an explicit HTML body: some captive-portal agents follow the
    redirect, others render whatever body comes back, so we provide both.
    """
    body = (
        f'<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0; url={portal_root}">'
        f'<title>ViaBot Survey Rig</title></head>'
        f'<body><a href="{portal_root}">Open the ViaBot survey dashboard</a></body></html>'
    )
    response = redirect(portal_root, code=302)
    response.set_data(body)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


def _build_allowed_hosts(config: Config) -> set[str]:
    """Hostnames that are really us, and so must not be portal-redirected."""
    import socket

    hostname = socket.gethostname().lower()
    hosts = {
        str(config["ap"]["address"]).lower(),
        str(config["web"]["portal_hostname"]).lower(),
        "localhost", "127.0.0.1", "[::1]", "::1",
        hostname, f"{hostname}.local",
    }
    # Reaching the dashboard over the wired LAN is handy for debugging.
    uplink = config["uplink"].get("interface")
    if uplink:
        from . import sysinfo
        address = sysinfo.interface_address(uplink)
        if address:
            hosts.add(address.lower())
    return {host for host in hosts if host}


def _drain(buffer: io.StringIO) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return value


# Timestamp formatting and the report itself live in report.py, which also
# renders them as a page. Re-exported here because the CSV exporters and the
# report API have used these names since the first week.
_iso = iso_local
_iso_utc = iso_utc
