"""SQLite 存储层: 建表与连接管理。"""
from __future__ import annotations

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    employee_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    submission_id TEXT NOT NULL UNIQUE,
    leave_type TEXT NOT NULL,
    start_date TEXT NOT NULL,   -- ISO YYYY-MM-DD, 不含时区
    end_date TEXT NOT NULL,
    status TEXT NOT NULL,       -- pending/effective/superseded/rejected
    base_version_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_versions_request ON versions(request_id, status);

CREATE TABLE IF NOT EXISTS approvals (
    submission_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL      -- approved/rejected, 一经产生不可改变
);

CREATE TABLE IF NOT EXISTS occupancy (
    employee_id TEXT NOT NULL,
    day TEXT NOT NULL,          -- ISO 日期, 覆盖生效与待审版本的并集
    request_id TEXT NOT NULL,
    PRIMARY KEY (employee_id, day)
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开(必要时创建)数据库并初始化 schema。"""
    conn = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """尽量启用 WAL 提升读写并发; 拿不到锁时降级为默认日志模式。

    正确性不依赖 WAL: 写事务统一走 BEGIN IMMEDIATE 串行化。
    """
    for _ in range(100):
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            time.sleep(0.05)


def next_id(conn: sqlite3.Connection, name: str, prefix: str) -> str:
    """在事务内取一个永久不复用的编号。"""
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, 0) "
        "ON CONFLICT(name) DO NOTHING",
        (name,),
    )
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (name,))
    value = conn.execute(
        "SELECT value FROM counters WHERE name = ?", (name,)
    ).fetchone()[0]
    return f"{prefix}{value:08d}"
