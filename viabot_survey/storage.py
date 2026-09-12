"""SQLite persistence for survey runs.

One database holds every run. Per-second measurements land in ``samples``;
sparse throughput tests and the dead zones found at the end of a run get
their own tables so the once-a-second row stays narrow.

All timestamps are Unix epoch seconds (UTC). The UI converts for display.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    label       TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    config_json TEXT,
    git_commit  TEXT,
    -- Recorded so a run stays interpretable if the Pi's timezone is changed
    -- later: video segment names are UTC, the burned-in clock is local.
    tz_name     TEXT,
    tz_offset_s INTEGER,
    -- Filled in when the run ends. Percentage is of time walked, not distance:
    -- there is no indoor positioning, which is why pausing while stationary
    -- matters to its accuracy.
    runnable_pct REAL,
    walked_s     REAL,
    dead_s       REAL,
    summary_json TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts              REAL NOT NULL,
    rtt_ms          REAL,
    loss_pct        REAL,
    jitter_ms       REAL,
    status          TEXT,
    dns_ms          REAL,
    rsrp            REAL,
    rsrq            REAL,
    sinr            REAL,
    rssi            REAL,
    band            TEXT,
    cell_id         TEXT,
    tech            TEXT,
    video_file      TEXT,
    video_offset_s  REAL,
    clock_synced    INTEGER,
    undervoltage    INTEGER,
    PRIMARY KEY (run_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_samples_run_ts ON samples(run_id, ts);

CREATE TABLE IF NOT EXISTS dead_zones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idx            INTEGER NOT NULL,
    start_ts       REAL NOT NULL,
    end_ts         REAL NOT NULL,
    duration_s     REAL NOT NULL,
    worst_loss_pct REAL,
    worst_rtt_ms   REAL,
    sample_count   INTEGER,
    video_file     TEXT,
    video_offset_s REAL,
    clip_path      TEXT,
    clip_error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dead_zones_run ON dead_zones(run_id, start_ts);

CREATE TABLE IF NOT EXISTS throughput (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts          REAL NOT NULL,
    down_mbps   REAL,
    up_mbps     REAL,
    retransmits INTEGER,
    bytes_used  INTEGER,
    server      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_throughput_run_ts ON throughput(run_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    source  TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- One row per file to be published, surviving reboots on purpose.
--
-- The rig loses power for real, and it may well lose it halfway through
-- sending a 60 MB clip over a cellular link. A queue held in memory would
-- forget the whole run; this one wakes up knowing exactly what it still owes.
--
-- sent_bytes is progress, not truth. The receiving end is the authority on how
-- much it actually has, because power can be cut between a chunk landing on
-- the server and the acknowledgement reaching the rig. Every resume starts by
-- asking.
CREATE TABLE IF NOT EXISTS uploads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- report | clip | video
    local_path  TEXT NOT NULL,
    remote_name TEXT NOT NULL,
    size_bytes  INTEGER,
    sent_bytes  INTEGER NOT NULL DEFAULT 0,
    -- held: queued but deliberately not sent yet (the full video, until asked
    -- for). pending: send it. done. failed: give up, with a reason.
    state       TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_ts  REAL NOT NULL,
    updated_ts  REAL NOT NULL,
    UNIQUE (run_id, remote_name)
);
CREATE INDEX IF NOT EXISTS idx_uploads_state ON uploads(state, id);
"""

SAMPLE_COLUMNS = (
    "rtt_ms", "loss_pct", "jitter_ms", "status", "dns_ms",
    "rsrp", "rsrq", "sinr", "rssi", "band", "cell_id", "tech",
    "video_file", "video_offset_s", "clock_synced", "undervoltage",
)


class Storage:
    """Thread-safe SQLite wrapper.

    Each thread gets its own connection (SQLite objects are not shareable
    across threads), and WAL mode lets the web request threads read while a
    worker thread is writing.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- connection handling -------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            # FULL, not NORMAL. Under NORMAL a commit is acknowledged before it
            # reaches the card, so a power cut loses transactions the app was
            # told had succeeded — which is how a completed walk came back with
            # its samples intact but no result recorded. This rig runs off a
            # battery through a screw-terminal splice and loses power for real,
            # so the durability is worth more than the speed. At one row a
            # second the cost is not measurable.
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _write(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.connection().execute(sql, tuple(params))

    # -- uploads -------------------------------------------------------------

    def enqueue_upload(self, run_id: str, kind: str, local_path: str,
                       remote_name: str, size_bytes: int | None = None,
                       held: bool = False) -> None:
        """Add a file to the publish queue, or leave an existing row alone.

        Re-analysing a run re-queues its report, and that must not undo the
        progress of an upload already halfway through.
        """
        now = time.time()
        self._write(
            """INSERT INTO uploads
                 (run_id, kind, local_path, remote_name, size_bytes, state,
                  created_ts, updated_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, remote_name) DO UPDATE SET
                 local_path = excluded.local_path,
                 size_bytes = excluded.size_bytes,
                 updated_ts = excluded.updated_ts,
                 -- A finished upload stays finished; a failed one gets
                 -- another chance now that something has changed.
                 state = CASE WHEN uploads.state = 'done' THEN 'done'
                              WHEN uploads.state = 'failed' THEN 'pending'
                              ELSE uploads.state END""",
            (run_id, kind, str(local_path), remote_name, size_bytes,
             "held" if held else "pending", now, now),
        )

    def pending_uploads(self, limit: int = 50) -> list[dict]:
        """Oldest first, so a run finishes publishing before the next starts."""
        rows = self.connection().execute(
            "SELECT * FROM uploads WHERE state = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def release_held_uploads(self, run_id: str, kind: str | None = None) -> int:
        """Move 'held' rows to 'pending' — someone asked for the full video."""
        sql = "UPDATE uploads SET state = 'pending', updated_ts = ? WHERE state = 'held' AND run_id = ?"
        params: list[Any] = [time.time(), run_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        return self._write(sql, params).rowcount

    def update_upload(self, upload_id: int, *, sent_bytes: int | None = None,
                      state: str | None = None, error: str | None = None,
                      bump_attempts: bool = False) -> None:
        sets = ["updated_ts = ?"]
        params: list[Any] = [time.time()]
        if sent_bytes is not None:
            sets.append("sent_bytes = ?")
            params.append(sent_bytes)
        if state is not None:
            sets.append("state = ?")
            params.append(state)
        sets.append("last_error = ?")
        params.append(error)
        if bump_attempts:
            sets.append("attempts = attempts + 1")
        params.append(upload_id)
        self._write(f"UPDATE uploads SET {', '.join(sets)} WHERE id = ?", params)

    def upload_summary(self, run_id: str | None = None) -> dict[str, Any]:
        """Counts by state, plus bytes still owed — what the dashboard shows."""
        where, params = ("WHERE run_id = ?", (run_id,)) if run_id else ("", ())
        rows = self.connection().execute(
            f"""SELECT state, COUNT(*) AS files,
                       SUM(COALESCE(size_bytes, 0) - sent_bytes) AS remaining
                FROM uploads {where} GROUP BY state""", params).fetchall()
        by_state = {r["state"]: {"files": r["files"],
                                 "remaining_bytes": max(0, r["remaining"] or 0)}
                    for r in rows}
        return {
            "by_state": by_state,
            "queued_files": by_state.get("pending", {}).get("files", 0),
            "queued_bytes": by_state.get("pending", {}).get("remaining_bytes", 0),
            "held_files": by_state.get("held", {}).get("files", 0),
            "failed_files": by_state.get("failed", {}).get("files", 0),
        }

    def runs_with_held_uploads(self) -> list[str]:
        """Runs whose full video is sitting on the rig, waiting to be asked for."""
        rows = self.connection().execute(
            "SELECT DISTINCT run_id FROM uploads WHERE state = 'held'").fetchall()
        return [row["run_id"] for row in rows]

    def list_uploads(self, run_id: str) -> list[dict]:
        rows = self.connection().execute(
            "SELECT * FROM uploads WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # -- runs ----------------------------------------------------------------

    def create_run(self, run_id: str, label: str = "", config: dict | None = None,
                   git_commit: str | None = None, started_at: float | None = None,
                   tz_name: str | None = None, tz_offset_s: int | None = None) -> dict:
        started = time.time() if started_at is None else started_at
        self._write(
            "INSERT INTO runs(id, started_at, label, config_json, git_commit, "
            "tz_name, tz_offset_s) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (run_id, started, label, json.dumps(config or {}), git_commit,
             tz_name, tz_offset_s),
        )
        return self.get_run(run_id)

    def end_run(self, run_id: str, ended_at: float | None = None) -> None:
        self._write("UPDATE runs SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (time.time() if ended_at is None else ended_at, run_id))

    def set_run_summary(self, run_id: str, summary: dict) -> None:
        self._write(
            "UPDATE runs SET runnable_pct = ?, walked_s = ?, dead_s = ?, "
            "summary_json = ? WHERE id = ?",
            (summary.get("runnable_pct"), summary.get("walked_s"),
             summary.get("dead_s"), json.dumps(summary), run_id),
        )

    def set_run_notes(self, run_id: str, notes: str) -> None:
        self._write("UPDATE runs SET notes = ? WHERE id = ?", (notes, run_id))

    def get_run(self, run_id: str) -> dict | None:
        row = self.connection().execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 100) -> list[dict]:
        rows = self.connection().execute(
            """
            SELECT r.*,
                   (SELECT COUNT(*) FROM samples s WHERE s.run_id = r.id) AS sample_count,
                   (SELECT COUNT(*) FROM dead_zones d WHERE d.run_id = r.id) AS dead_zone_count
            FROM runs r ORDER BY r.started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_run(self) -> dict | None:
        row = self.connection().execute(
            "SELECT * FROM runs WHERE ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def delete_run(self, run_id: str) -> None:
        self._write("DELETE FROM runs WHERE id = ?", (run_id,))

    # -- samples -------------------------------------------------------------

    def add_sample(self, run_id: str, ts: float, **fields: Any) -> None:
        unknown = set(fields) - set(SAMPLE_COLUMNS)
        if unknown:
            raise ValueError(f"unknown sample columns: {sorted(unknown)}")
        columns = ["run_id", "ts", *fields]
        placeholders = ", ".join("?" * len(columns))
        self._write(
            f"INSERT OR REPLACE INTO samples({', '.join(columns)}) VALUES({placeholders})",
            (run_id, ts, *fields.values()),
        )

    def recent_samples(self, run_id: str, seconds: float, now: float | None = None) -> list[dict]:
        cutoff = (time.time() if now is None else now) - seconds
        rows = self.connection().execute(
            "SELECT * FROM samples WHERE run_id = ? AND ts >= ? ORDER BY ts",
            (run_id, cutoff),
        ).fetchall()
        return [dict(row) for row in rows]

    def iter_samples(self, run_id: str) -> Iterator[dict]:
        cursor = self.connection().execute(
            "SELECT * FROM samples WHERE run_id = ? ORDER BY ts", (run_id,))
        for row in cursor:
            yield dict(row)

    # -- dead zones ----------------------------------------------------------

    def replace_dead_zones(self, run_id: str, zones: list[dict]) -> None:
        """Store a run's dead zones, replacing any previous analysis.

        Detection runs off stored samples, so re-analysing a run after changing
        the thresholds is expected and must not accumulate duplicates.
        """
        with self._write_lock:
            conn = self.connection()
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM dead_zones WHERE run_id = ?", (run_id,))
                for zone in zones:
                    conn.execute(
                        "INSERT INTO dead_zones(run_id, idx, start_ts, end_ts, "
                        "duration_s, worst_loss_pct, worst_rtt_ms, sample_count, "
                        "video_file, video_offset_s, clip_path, clip_error) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_id, zone["index"], zone["start_ts"], zone["end_ts"],
                         zone["duration_s"], zone.get("worst_loss_pct"),
                         zone.get("worst_rtt_ms"), zone.get("sample_count"),
                         zone.get("video_file"), zone.get("video_offset_s"),
                         zone.get("clip_path"), zone.get("clip_error")))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def list_dead_zones(self, run_id: str) -> list[dict]:
        rows = self.connection().execute(
            "SELECT * FROM dead_zones WHERE run_id = ? ORDER BY start_ts",
            (run_id,)).fetchall()
        return [dict(row) for row in rows]

    # -- throughput ----------------------------------------------------------

    def add_throughput(self, run_id: str, ts: float, **fields: Any) -> None:
        allowed = {"down_mbps", "up_mbps", "retransmits", "bytes_used", "server", "error"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown throughput columns: {sorted(unknown)}")
        columns = ["run_id", "ts", *fields]
        placeholders = ", ".join("?" * len(columns))
        self._write(
            f"INSERT INTO throughput({', '.join(columns)}) VALUES({placeholders})",
            (run_id, ts, *fields.values()),
        )

    def list_throughput(self, run_id: str) -> list[dict]:
        rows = self.connection().execute(
            "SELECT * FROM throughput WHERE run_id = ? ORDER BY ts", (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def run_data_used_bytes(self, run_id: str) -> int:
        row = self.connection().execute(
            "SELECT COALESCE(SUM(bytes_used), 0) AS total FROM throughput WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return int(row["total"])

    # -- events --------------------------------------------------------------

    def add_event(self, message: str, level: str = "info", source: str = "",
                  run_id: str | None = None, ts: float | None = None) -> None:
        self._write(
            "INSERT INTO events(run_id, ts, level, source, message) VALUES(?, ?, ?, ?, ?)",
            (run_id, time.time() if ts is None else ts, level, source, message),
        )

    def recent_events(self, limit: int = 50, run_id: str | None = None) -> list[dict]:
        if run_id is None:
            sql = "SELECT * FROM events ORDER BY ts DESC LIMIT ?"
            params: tuple = (limit,)
        else:
            sql = "SELECT * FROM events WHERE run_id = ? ORDER BY ts DESC LIMIT ?"
            params = (run_id, limit)
        return [dict(row) for row in self.connection().execute(sql, params).fetchall()]
