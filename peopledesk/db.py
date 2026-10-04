"""SQLite schema and connection helpers."""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    employee_id  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version_id       TEXT PRIMARY KEY,
    request_id       TEXT NOT NULL REFERENCES requests(request_id),
    leave_type       TEXT NOT NULL,
    start_date       TEXT NOT NULL,
    end_date         TEXT NOT NULL,
    status           TEXT NOT NULL
                     CHECK (status IN ('pending','effective','superseded','rejected')),
    base_version_id  TEXT REFERENCES versions(version_id),
    submission_id    TEXT NOT NULL UNIQUE,
    seq              INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS version_usage (
    version_id  TEXT NOT NULL REFERENCES versions(version_id),
    year        INTEGER NOT NULL,
    days        INTEGER NOT NULL CHECK (days >= 0),
    PRIMARY KEY (version_id, year)
);

CREATE TABLE IF NOT EXISTS occupancy (
    employee_id  TEXT NOT NULL,
    date         TEXT NOT NULL,
    request_id   TEXT NOT NULL REFERENCES requests(request_id),
    PRIMARY KEY (employee_id, date)
);

CREATE TABLE IF NOT EXISTS approvals (
    submission_id  TEXT PRIMARY KEY,
    verdict        TEXT NOT NULL CHECK (verdict IN ('approved','rejected'))
);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open (and initialize) the database file."""
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
