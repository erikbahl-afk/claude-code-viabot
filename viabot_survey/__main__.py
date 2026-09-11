"""Entry point: ``python -m viabot_survey`` (what the systemd unit runs)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from . import __version__, config as config_module
from .app import create_app
from .runner import SurveyRunner
from .storage import Storage
from .updater import Updater

log = logging.getLogger("viabot_survey")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="viabot-survey",
                                     description="ViaBot garage coverage survey rig")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.yaml (default: config/config.yaml)")
    parser.add_argument("--host", default=None, help="override web.host")
    parser.add_argument("--port", type=int, default=None, help="override web.port")
    parser.add_argument("--no-captive-portal", action="store_true",
                        help="serve the dashboard without redirecting unknown hosts")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = config_module.load(args.config)
    for warning in cfg.warnings:
        log.warning("config: %s", warning)
    if cfg.source is None:
        log.warning("no config/config.yaml found — running on example defaults. "
                    "Copy config/config.example.yaml to config/config.yaml.")

    if args.host:
        cfg["web"]["host"] = args.host
    if args.port:
        cfg["web"]["port"] = args.port
    if args.no_captive_portal:
        cfg["web"]["captive_portal"] = False

    storage = Storage(cfg.db_path)
    runner = SurveyRunner(cfg, storage)
    updater = Updater(cfg.repo_root, remote=cfg["update"]["remote"],
                      branch=cfg["update"]["branch"], enabled=cfg["update"]["enabled"])
    app = create_app(cfg, runner, storage, updater)

    runner.start()

    def _shutdown(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        runner.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    host, port = cfg["web"]["host"], int(cfg["web"]["port"])
    log.info("dashboard on http://%s:%s/ (AP address %s)",
             host, port, cfg["ap"]["address"])
    try:
        _serve(app, host, port)
    finally:
        runner.shutdown()
    return 0


def _serve(app, host: str, port: int) -> None:
    """Prefer waitress; fall back to Werkzeug so a bare checkout still runs."""
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        log.warning("waitress not installed, using the Werkzeug server "
                    "(fine for one operator, but run scripts/setup.sh for the real thing)")
        app.run(host=host, port=port, threaded=True, debug=False,
                use_reloader=False)
        return
    # A handful of threads is plenty: one operator, polling once a second.
    waitress_serve(app, host=host, port=port, threads=8, ident="viabot-survey",
                   clear_untrusted_proxy_headers=True)


if __name__ == "__main__":
    raise SystemExit(main())
