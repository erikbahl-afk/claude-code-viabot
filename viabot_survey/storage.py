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

SCHEMA_VERSION = 1

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
            conn.execute("PRAGMA synchronous=NORMAL")
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
