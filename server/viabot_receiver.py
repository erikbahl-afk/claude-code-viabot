"""Receives finished surveys from the rigs and serves them as web pages.

Runs on a small cloud box. One file, one dependency (Flask), no database — a
survey is a directory of files, which is the right shape for something whose
job is to accept files and hand them back.

Two things here are less obvious than they look.

**Uploads are resumable at any byte.** A rig may lose power mid-chunk, and its
link is the cellular connection it went out to measure. Each file is appended
to a ``.part`` alongside its final name, and the size of that part *is* the
resume offset — no bookkeeping to get out of step with the bytes on disk. A
chunk that does not continue exactly where the part ends is refused with the
real offset, which turns a duplicated or reordered chunk into a no-op instead
of a corrupted file.

**Reports are not public.** Run identifiers are readable by design
(``20250911-194640-level-2-garage``), so a link is guessable and the contents
name a customer's site. Everything therefore sits behind a password, with a
per-run share key as the exception: it opens exactly one report, so a customer
can be sent a link without being handed the archive.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, request,
                   send_from_directory)

#: Where runs are kept. One directory per run, files inside named as uploaded.
DATA_DIR = Path(os.environ.get("VIABOT_RECEIVER_DATA", "/var/lib/viabot-receiver"))

#: Shared secret the rigs authenticate with. No default: an unset token would
#: otherwise silently accept uploads from anyone who found the address.
UPLOAD_TOKEN = os.environ.get("VIABOT_RECEIVER_TOKEN", "")

#: Password for reading reports in a browser. Username is ignored.
VIEWER_PASSWORD = os.environ.get("VIABOT_RECEIVER_VIEWER_PASSWORD", "")

#: Refuse a single file larger than this. A full walk video is a few hundred
#: megabytes; a few gigabytes means something has gone wrong.
MAX_FILE_BYTES = int(os.environ.get("VIABOT_RECEIVER_MAX_BYTES", 8 * 1024 ** 3))

#: One path segment: letters, digits and the punctuation filenames really use.
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

app = Flask(__name__)

#: One lock per file being received. Two chunks arriving together would
#: otherwise both read the same offset, both pass the check, and both append —
#: which is how a retry over a flaky link turns into a longer, broken file.
_file_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def file_lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _file_locks[key]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def safe_relative(name: str, depth: int = 3) -> Path:
    """Validate an uploaded path, or refuse it.

    Allowing "../" here would let a rig — or anyone holding its token — write
    anywhere the service can reach, so this is an allow-list of shapes rather
    than a search for bad ones.
    """
    parts = [p for p in str(name).split("/") if p not in ("", ".")]
    if not parts or len(parts) > depth:
        abort(400, "bad path")
    for part in parts:
        if not SEGMENT.match(part):
            abort(400, "bad path")
    return Path(*parts)


def run_dir(run_id: str, create: bool = False) -> Path:
    safe_relative(run_id, depth=1)
    path = DATA_DIR / "runs" / run_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def meta_path(run_id: str) -> Path:
    return run_dir(run_id) / "_meta.json"


def read_meta(run_id: str) -> dict:
    try:
        return json.loads(meta_path(run_id).read_text())
    except (OSError, ValueError):
        return {}


def write_meta(run_id: str, meta: dict) -> None:
    path = meta_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def require_rig() -> None:
    """Authenticate an uploading rig."""
    if not UPLOAD_TOKEN:
        abort(503, "VIABOT_RECEIVER_TOKEN is not set on the server")
    header = request.headers.get("Authorization", "")
    presented = header[7:] if header.startswith("Bearer ") else ""
    # Constant time: a token is guessable one character at a time otherwise.
    if not hmac.compare_digest(presented, UPLOAD_TOKEN):
        abort(401, "bad token")


def may_view(run_id: str | None = None) -> bool:
    """Whether this request may read reports.

    Either the viewer password, or a share key that opens one run — which is
    how a single report is sent to a customer without handing over the rest.
    """
    if not VIEWER_PASSWORD:
        return True                      # explicitly unprotected; see the README
    auth = request.authorization
    if auth and hmac.compare_digest(auth.password or "", VIEWER_PASSWORD):
        return True
    key = request.args.get("k", "")
    if run_id and key:
        expected = read_meta(run_id).get("share_key", "")
        if expected and hmac.compare_digest(key, expected):
            return True
    return False


def require_viewer(run_id: str | None = None) -> None:
    if not may_view(run_id):
        abort(Response("Authentication required", 401,
                       {"WWW-Authenticate": 'Basic realm="ViaBot surveys"'}))


# ---------------------------------------------------------------------------
# Upload protocol
# ---------------------------------------------------------------------------

def part_of(target: Path) -> Path:
    return target.with_name(target.name + ".part")


def current_offset(target: Path) -> tuple[int, bool]:
    """Bytes held for this file, and whether it is finished.

    The answer comes from the size of what is on disk rather than from any
    record kept alongside it. A separate record can disagree with the bytes
    after a crash; a file's own length cannot.
    """
    if target.exists():
        return target.stat().st_size, True
    part = part_of(target)
    return (part.stat().st_size, False) if part.exists() else (0, False)


@app.route("/api/v1/runs/<run_id>/files/<path:name>", methods=["HEAD", "GET"])
def file_status(run_id: str, name: str):
    require_rig()
    target = run_dir(run_id) / safe_relative(name)
    offset, complete = current_offset(target)
    if offset == 0 and not complete:
        return Response(status=404, headers={"Upload-Offset": "0"})
    return Response(status=200, headers={
        "Upload-Offset": str(offset),
        "Upload-Complete": "1" if complete else "0",
    })


@app.route("/api/v1/runs/<run_id>/files/<path:name>", methods=["PATCH"])
def file_append(run_id: str, name: str):
    """Append one chunk, but only if it continues exactly where we left off."""
    require_rig()
    relative = safe_relative(name)
    target = run_dir(run_id, create=True) / relative
    target.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(f"{run_id}/{relative}"):
        return _append_chunk(target)


def _append_chunk(target: Path):
    offset, complete = current_offset(target)
    if complete:
        return Response(status=200, headers={"Upload-Offset": str(offset),
                                             "Upload-Complete": "1"})

    try:
        claimed = int(request.headers.get("Upload-Offset", "0"))
        total = int(request.headers.get("Upload-Length", "0"))
    except ValueError:
        abort(400, "bad offset headers")

    if claimed != offset:
        # Not an error — this is what a resume looks like when the rig's own
        # record was stale. Tell it the truth and let it seek.
        return Response("offset mismatch", status=409,
                        headers={"Upload-Offset": str(offset)})

    payload = request.get_data(cache=False)
    if total > MAX_FILE_BYTES or offset + len(payload) > MAX_FILE_BYTES:
        abort(413, "file too large")

    part = part_of(target)
    with part.open("ab") as handle:
        handle.write(payload)
        handle.flush()
        # The server can lose power too, and an acknowledged chunk that is not
        # on the disk would make the rig skip bytes it never actually sent.
        os.fsync(handle.fileno())
    offset = part.stat().st_size

    if total and offset >= total:
        part.replace(target)
        _fsync_dir(target.parent)
        return Response(status=200, headers={"Upload-Offset": str(offset),
                                             "Upload-Complete": "1"})
    return Response(status=204, headers={"Upload-Offset": str(offset),
                                         "Upload-Complete": "0"})


def _fsync_dir(path: Path) -> None:
    """Make a rename durable. Without this the file can vanish on power loss
    even though its contents were safely written."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


@app.route("/api/v1/runs/<run_id>/manifest", methods=["POST"])
def put_manifest(run_id: str):
    """Record what a run is. Creates its share key the first time."""
    require_rig()
    run_dir(run_id, create=True)
    meta = read_meta(run_id)
    incoming = request.get_json(silent=True) or {}
    meta.update({k: v for k, v in incoming.items()
                 if k not in ("share_key", "requests")})
    meta.setdefault("share_key", secrets.token_urlsafe(16))
    meta.setdefault("first_seen", time.time())
    meta["updated"] = time.time()
    write_meta(run_id, meta)
    return jsonify({"ok": True, "share_key": meta["share_key"]})


@app.route("/api/v1/requests")
def all_requests():
    """Everything anyone has asked for, across every run, in one answer.

    The rig used to ask per run, which meant one HTTP request per survey it
    still held a video for, every polling cycle, forever. After a few months of
    walks that is a steady stream of requests about runs nobody will ever ask
    about again.
    """
    out = {}
    root = DATA_DIR / "runs"
    if root.is_dir():
        for path in root.iterdir():
            if not path.is_dir():
                continue
            wanted = read_meta(path.name).get("requests") or {}
            if any(v for k, v in wanted.items() if k != "asked_at"):
                out[path.name] = wanted
    return jsonify(out)


@app.route("/api/v1/runs/<run_id>/requests")
def run_requests(run_id: str):
    """What a reader asked for. The rig polls this; nothing can reach it."""
    require_rig()
    return jsonify(read_meta(run_id).get("requests", {}))


@app.route("/api/v1/runs/<run_id>/requests", methods=["DELETE"])
def clear_requests(run_id: str):
    require_rig()
    meta = read_meta(run_id)
    meta["requests"] = {}
    write_meta(run_id, meta)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    require_viewer()
    runs = []
    root = DATA_DIR / "runs"
    if root.is_dir():
        for path in sorted(root.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            meta = read_meta(path.name)
            runs.append({"id": path.name, **meta,
                         "ready": (path / "index.html").exists()})
    return Response(_index_html(runs), mimetype="text/html; charset=utf-8")


@app.route("/r/<run_id>/")
def report_root(run_id: str):
    require_viewer(run_id)
    if not (run_dir(run_id) / "index.html").exists():
        return Response(_waiting_html(run_id, read_meta(run_id)),
                        mimetype="text/html; charset=utf-8", status=202)
    return send_from_directory(run_dir(run_id), "index.html")


@app.route("/r/<run_id>/<path:name>")
def report_file(run_id: str, name: str):
    require_viewer(run_id)
    relative = safe_relative(name)
    directory = run_dir(run_id)
    if not (directory / relative).exists():
        abort(404)
    # conditional=True gives byte-range requests, which is what lets a browser
    # scrub through a video instead of downloading all of it first.
    return send_from_directory(directory, str(relative), conditional=True)


@app.route("/r/<run_id>/request-video", methods=["POST"])
def request_video(run_id: str):
    """The button on the report's second tab.

    Records the ask; the rig picks it up next time it is powered on and not
    walking. There is no way to reach the rig directly — it sits behind carrier
    NAT — so this is a note left where it will look.
    """
    require_viewer(run_id)
    meta = read_meta(run_id)
    if not meta:
        abort(404)
    suffix = f"?k={request.args['k']}" if request.args.get("k") else ""
    back = f"/r/{run_id}/{suffix}#detail"

    # Already here: the rig sent it and the reader pressed the button anyway,
    # which is exactly what someone does when nothing seems to happen.
    if (run_dir(run_id) / "video" / "full.mp4").exists():
        return Response(_requested_html(run_id, meta, back, already=True),
                        mimetype="text/html; charset=utf-8")

    requests_ = meta.setdefault("requests", {})
    requests_["full_video"] = True
    requests_["asked_at"] = time.time()
    write_meta(run_id, meta)
    # Not a bare redirect back to the same page. That is what this used to do,
    # and the page is the static report the rig uploaded — so it came back
    # looking identical, with the same button, and the only way to tell the
    # press had worked was to read a database on the rig.
    return Response(_requested_html(run_id, meta, back),
                    mimetype="text/html; charset=utf-8")


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "runs": len(list((DATA_DIR / "runs").glob("*")))
                    if (DATA_DIR / "runs").is_dir() else 0})


def _esc(value: object) -> str:
    from html import escape
    return escape("" if value is None else str(value), quote=True)


_PAGE_CSS = """
body{margin:0;background:#fbfcfd;color:#16181d;font:15px/1.55 -apple-system,
BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:880px;margin:0 auto;padding:36px 20px 64px}
h1{font-size:24px;margin:0 0 20px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e3e6ec;
border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid #e3e6ec}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#646b7a}
tr:last-child td{border-bottom:0}
a{color:#1b4d8f}
.muted{color:#646b7a}
.empty{background:#fff;border:1px solid #e3e6ec;border-radius:10px;padding:28px;
text-align:center;color:#646b7a}
"""


def _index_html(runs: list[dict]) -> str:
    if not runs:
        body = ('<div class="empty">No surveys yet. A rig publishes one when a '
                "walk finishes.</div>")
    else:
        rows = []
        for run in runs:
            pct = run.get("runnable_pct")
            started = run.get("started_at")
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
                    if started else "")
            link = f'/r/{_esc(run["id"])}/'
            share = f'{link}?k={_esc(run.get("share_key"))}'
            rows.append(
                f'<tr><td><a href="{link}">{_esc(run.get("label") or run["id"])}</a>'
                f'<br><span class="muted">{_esc(run["id"])}</span></td>'
                f'<td>{_esc(when)}</td>'
                f'<td>{"—" if pct is None else _esc(f"{pct:g}%")}</td>'
                f'<td>{_esc(run.get("dead_zone_count", "—"))}</td>'
                f'<td>{"" if run.get("ready") else "<span class=muted>uploading…</span>"}'
                f'<br><a href="{share}">share link</a></td></tr>')
        body = ('<table><thead><tr><th>Survey</th><th>Walked</th>'
                '<th>Runnable</th><th>Dead zones</th><th></th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table>'
                '<p class="muted">A share link opens that one survey without '
                'the password — send it to a customer, not the address above.</p>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>ViaBot coverage surveys</title><style>{_PAGE_CSS}</style></head>'
            f'<body><div class="wrap"><h1>Coverage surveys</h1>{body}</div></body></html>')


def _requested_html(run_id: str, meta: dict, back: str,
                    already: bool = False) -> str:
    """Say the press landed. Anything less and it gets pressed again."""
    label = meta.get("label") or run_id
    if already:
        headline = "The full video is already here."
        # Not "go and look at the report". A report published before the page
        # learned to check for the video cannot ever show it: uploads are final
        # once complete, so that page is frozen as it was written. Hand over
        # the file itself, which works whatever the report looks like.
        detail = ("It finished uploading earlier. Reports written before "
                  "2026-09-17 cannot show it inline — a published report "
                  "cannot be changed — so use the link below.")
    else:
        headline = "Asked for."
        detail = ("The rig picks this up the next time it is powered on and "
                  "not walking, then uploads the whole recording over the same "
                  "cellular link it measures — several minutes for a long "
                  "walk. Nothing more to do: the report shows the video as "
                  "soon as it arrives.")
    watch = ""
    if already:
        key = f"?k={request.args['k']}" if request.args.get("k") else ""
        watch = (f'<p><a href="/r/{_esc(run_id)}/video/full.mp4{_esc(key)}">'
                 "Watch the full recording</a></p>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{_esc(label)} — full video</title>'
            f'<style>{_PAGE_CSS}</style></head><body><div class="wrap">'
            f'<h1>{_esc(headline)}</h1><p>{_esc(detail)}</p>{watch}'
            f'<p><a href="{_esc(back)}">Back to the report</a></p>'
            f'</div></body></html>')


def _waiting_html(run_id: str, meta: dict) -> str:
    label = meta.get("label") or run_id
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="30">'
            f'<title>{_esc(label)} — uploading</title><style>{_PAGE_CSS}</style>'
            f'</head><body><div class="wrap"><h1>{_esc(label)}</h1>'
            f'<div class="empty"><p>This survey is still uploading.</p>'
            f'<p>The rig sends it over the cellular link it was measuring, and '
            f'continues where it left off if it loses power. This page refreshes '
            f'itself.</p></div></div></body></html>')


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="ViaBot survey receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8089)
    args = parser.parse_args()

    if not UPLOAD_TOKEN:
        print("refusing to start: set VIABOT_RECEIVER_TOKEN", flush=True)
        return 2
    if not VIEWER_PASSWORD and not os.environ.get("VIABOT_RECEIVER_ALLOW_PUBLIC"):
        # Reports name a customer's site and a run id is guessable by design, so
        # "nobody will find it" is not access control. Refusing is louder than a
        # warning nobody reads in a unit's startup log.
        print("refusing to start: VIABOT_RECEIVER_VIEWER_PASSWORD is unset, so "
              "every survey would be readable by anyone who can reach this "
              "service. Set it, or set VIABOT_RECEIVER_ALLOW_PUBLIC=1 if that "
              "is genuinely what you want.", flush=True)
        return 2
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
