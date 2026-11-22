"""SQLite 存储层。

设计要点：
- 每个版本的数据整体写入、永不修改（immutable），通过 meta 表中的
  current_version_id 指针标识当前正式版。
- 发布 = 单个事务内插入全部数据 + 拨动指针，对查询方而言要么看到
  旧完整版本、要么看到新完整版本，绝不出现半新半旧。
- 失败版本只写 versions/version_errors 元数据，不写任何时刻数据，
  也不触碰指针，当前正式版保持不变。
- WAL 模式：写入期间不阻塞读取。
"""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "data", "gtfs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('published', 'failed')),
    error_count  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stops (
    version_id INTEGER NOT NULL,
    stop_id    TEXT NOT NULL,
    stop_name  TEXT NOT NULL DEFAULT '',
    stop_lat   REAL,
    stop_lon   REAL,
    PRIMARY KEY (version_id, stop_id)
);

CREATE TABLE IF NOT EXISTS routes (
    version_id       INTEGER NOT NULL,
    route_id         TEXT NOT NULL,
    route_short_name TEXT NOT NULL DEFAULT '',
    route_long_name  TEXT NOT NULL DEFAULT '',
    route_type       INTEGER,
    PRIMARY KEY (version_id, route_id)
);

CREATE TABLE IF NOT EXISTS calendar (
    version_id INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    monday     INTEGER NOT NULL,
    tuesday    INTEGER NOT NULL,
    wednesday  INTEGER NOT NULL,
    thursday   INTEGER NOT NULL,
    friday     INTEGER NOT NULL,
    saturday   INTEGER NOT NULL,
    sunday     INTEGER NOT NULL,
    start_date TEXT NOT NULL,   -- ISO YYYY-MM-DD，可字典序比较
    end_date   TEXT NOT NULL,
    PRIMARY KEY (version_id, service_id)
);

CREATE TABLE IF NOT EXISTS trips (
    version_id    INTEGER NOT NULL,
    route_id      TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    trip_id       TEXT NOT NULL,
    trip_headsign TEXT NOT NULL DEFAULT '',
    direction_id  INTEGER,
    PRIMARY KEY (version_id, trip_id)
);
CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (version_id, route_id);

CREATE TABLE IF NOT EXISTS stop_times (
    version_id     INTEGER NOT NULL,
    trip_id        TEXT NOT NULL,
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT NOT NULL,
    arrival_secs   INTEGER NOT NULL,   -- 自运营日 00:00 起的秒数，可 >= 86400
    departure_secs INTEGER NOT NULL,
    PRIMARY KEY (version_id, trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times (version_id, stop_id);

CREATE TABLE IF NOT EXISTS version_errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    file       TEXT NOT NULL,
    row        INTEGER,
    field      TEXT,
    message    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_errors ON version_errors (version_id);

-- 一趟班次（版本 + trip + 运营日）的整份实到更正：再次上传即整份替换
CREATE TABLE IF NOT EXISTS actual_uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id    INTEGER NOT NULL,
    trip_id       TEXT NOT NULL,
    service_date  TEXT NOT NULL,   -- ISO YYYY-MM-DD 运营日
    feasible      INTEGER NOT NULL,
    located_count INTEGER NOT NULL,
    pending_count INTEGER NOT NULL,
    uploaded_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (version_id, trip_id, service_date)
);

-- 实到明细 + 对齐结果；统计与明细都读这一份已保存数据
CREATE TABLE IF NOT EXISTS actual_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    upload_id           INTEGER NOT NULL,
    csv_row             INTEGER NOT NULL,
    stop_id             TEXT NOT NULL,
    actual_secs         INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('located', 'pending')),
    stop_sequence       INTEGER,
    diff_secs           INTEGER,
    on_time             INTEGER,
    candidate_sequences TEXT,       -- 待核行的候选停站序号 JSON；已定位为 NULL
    FOREIGN KEY (upload_id) REFERENCES actual_uploads(id)
);
CREATE INDEX IF NOT EXISTS idx_actual_records_upload ON actual_records (upload_id);
CREATE INDEX IF NOT EXISTS idx_actual_uploads_trip
    ON actual_uploads (version_id, trip_id, service_date);
CREATE INDEX IF NOT EXISTS idx_actual_records_stop
    ON actual_records (upload_id, stop_id, status);
"""


def db_path() -> str:
    return os.environ.get("GTFS_DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def current_version_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'current_version_id'").fetchone()
    return int(row["value"]) if row else None


def publish_version(conn: sqlite3.Connection, label: str,
                    tables: dict[str, list[tuple]]) -> int:
    """单事务写入整版数据并拨动指针。调用方保证 tables 已通过校验。"""
    with conn:  # 事务：全部成功才提交，异常自动回滚
        cur = conn.execute(
            "INSERT INTO versions (label, status, published_at) "
            "VALUES (?, 'published', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (label,))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO stops (version_id, stop_id, stop_name, stop_lat, stop_lon) "
            "VALUES (?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["stops"]])
        conn.executemany(
            "INSERT INTO routes (version_id, route_id, route_short_name, "
            "route_long_name, route_type) VALUES (?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["routes"]])
        conn.executemany(
            "INSERT INTO calendar (version_id, service_id, monday, tuesday, "
            "wednesday, thursday, friday, saturday, sunday, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["calendar"]])
        conn.executemany(
            "INSERT INTO trips (version_id, route_id, service_id, trip_id, "
            "trip_headsign, direction_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["trips"]])
        conn.executemany(
            "INSERT INTO stop_times (version_id, trip_id, stop_sequence, stop_id, "
            "arrival_secs, departure_secs) VALUES (?, ?, ?, ?, ?, ?)",
            [(vid, *r) for r in tables["stop_times"]])
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('current_version_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(vid),))
    return vid


def record_failed_version(conn: sqlite3.Connection, label: str,
                          errors: list[dict]) -> int:
    """记录失败版本及其错误明细；不触碰正式版指针。"""
    with conn:
        cur = conn.execute(
            "INSERT INTO versions (label, status, error_count) VALUES (?, 'failed', ?)",
            (label, len(errors)))
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO version_errors (version_id, file, row, field, message) "
            "VALUES (?, ?, ?, ?, ?)",
            [(vid, e["file"], e["row"], e["field"], e["message"]) for e in errors])
    return vid


# ---------------- 实到时刻更正 ----------------

def replace_actual_upload(conn: sqlite3.Connection, version_id: int, trip_id: str,
                          service_date: str, feasible: bool,
                          results: list[dict]) -> int:
    """单事务整份保存一趟班次的实到更正。

    同 (version_id, trip_id, service_date) 已有记录时整份替换：
    先删旧 upload/records，再写新数据；任何异常整体回滚，旧数据保留。
    """
    import json

    located = [r for r in results if r["status"] == "located"]
    with conn:
        old = conn.execute(
            "SELECT id FROM actual_uploads WHERE version_id = ? AND trip_id = ? "
            "AND service_date = ?", (version_id, trip_id, service_date)).fetchone()
        if old is not None:
            conn.execute("DELETE FROM actual_records WHERE upload_id = ?", (old["id"],))
            conn.execute("DELETE FROM actual_uploads WHERE id = ?", (old["id"],))
        cur = conn.execute(
            "INSERT INTO actual_uploads (version_id, trip_id, service_date, feasible, "
            "located_count, pending_count) VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, trip_id, service_date, 1 if feasible else 0,
             len(located), len(results) - len(located)))
        upload_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO actual_records (upload_id, csv_row, stop_id, actual_secs, "
            "status, stop_sequence, diff_secs, on_time, candidate_sequences) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(upload_id, r["csv_row"], r["stop_id"], r["actual_secs"], r["status"],
              r["stop_sequence"], r["diff_secs"],
              None if r["on_time"] is None else (1 if r["on_time"] else 0),
              None if r["candidate_sequences"] is None
              else json.dumps(r["candidate_sequences"]))
             for r in results])
    return upload_id


def find_actual_upload(conn: sqlite3.Connection, version_id: int, trip_id: str,
                       service_date: str):
    return conn.execute(
        "SELECT * FROM actual_uploads WHERE version_id = ? AND trip_id = ? "
        "AND service_date = ?", (version_id, trip_id, service_date)).fetchone()


def get_actual_upload(conn: sqlite3.Connection, upload_id: int):
    return conn.execute(
        "SELECT * FROM actual_uploads WHERE id = ?", (upload_id,)).fetchone()


def list_actual_uploads(conn: sqlite3.Connection, version_id: int | None = None,
                        route_id: str | None = None,
                        service_date: str | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT u.* FROM actual_uploads u")
    params: list = []
    where = []
    if route_id is not None:
        sql += " JOIN trips t ON t.version_id = u.version_id AND t.trip_id = u.trip_id"
        where.append("t.route_id = ?")
        params.append(route_id)
    if version_id is not None:
        where.append("u.version_id = ?")
        params.append(version_id)
    if service_date is not None:
        where.append("u.service_date = ?")
        params.append(service_date)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY u.service_date, u.trip_id, u.id"
    return conn.execute(sql, params).fetchall()


def list_actual_records(conn: sqlite3.Connection, upload_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM actual_records WHERE upload_id = ? "
        "ORDER BY actual_secs, csv_row", (upload_id,)).fetchall()


def stop_punctuality_stats(conn: sqlite3.Connection, version_id: int,
                           route_id: str | None = None,
                           service_date: str | None = None) -> list[sqlite3.Row]:
    """按站汇总已定位（located）记录条数与准点率。待核行不计入。"""
    sql = (
        "SELECT r.stop_id, s.stop_name, "
        "COUNT(*) AS located_count, "
        "SUM(r.on_time) AS on_time_count, "
        "ROUND(100.0 * SUM(r.on_time) / COUNT(*), 2) AS on_time_rate "
        "FROM actual_records r "
        "JOIN actual_uploads u ON u.id = r.upload_id "
        "LEFT JOIN stops s ON s.version_id = u.version_id AND s.stop_id = r.stop_id "
        "WHERE r.status = 'located' AND u.version_id = ?")
    params: list = [version_id]
    if route_id is not None:
        sql += (" AND u.trip_id IN (SELECT trip_id FROM trips "
                "WHERE version_id = ? AND route_id = ?)")
        params.extend([version_id, route_id])
    if service_date is not None:
        sql += " AND u.service_date = ?"
        params.append(service_date)
    sql += " GROUP BY r.stop_id ORDER BY r.stop_id"
    return conn.execute(sql, params).fetchall()
