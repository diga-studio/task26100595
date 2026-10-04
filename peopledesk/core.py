"""PeopleDesk Lite: leave accounting on top of a single SQLite file."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from typing import Optional

from . import dates as _dates
from .db import connect
from .errors import (
    ConflictError,
    DateOverlapError,
    NotFoundError,
    QuotaExceededError,
)

LEAVE_TYPES = ("annual", "sick", "personal", "unpaid")

YEARLY_QUOTA = {
    "annual": 15,
    "sick": 10,
    "personal": 3,
    "unpaid": None,  # unlimited
}


class PeopleDesk:
    """Main entry point. All state lives in one SQLite file."""

    def __init__(self, db_path: str):
        self._conn = connect(db_path)

    # -- transaction helper --------------------------------------------
    @contextmanager
    def _tx(self):
        """Serialized read-write transaction (BEGIN IMMEDIATE)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- internal helpers -----------------------------------------------
    def _next_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM versions"
        ).fetchone()
        return row[0]

    def _insert_version(self, request_id, leave_type, start, end,
                        base_version_id, submission_id) -> str:
        version_id = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO versions (version_id, request_id, leave_type,"
            " start_date, end_date, status, base_version_id,"
            " submission_id, seq)"
            " VALUES (?,?,?,?,?,'pending',?,?,?)",
            (version_id, request_id, leave_type, start.isoformat(),
             end.isoformat(), base_version_id, submission_id,
             self._next_seq()),
        )
        for year, days in _dates.chargeable_days_by_year(start, end).items():
            self._conn.execute(
                "INSERT INTO version_usage (version_id, year, days)"
                " VALUES (?,?,?)",
                (version_id, year, days),
            )
        return version_id

    def _version_usage(self, version_id) -> dict[int, int]:
        rows = self._conn.execute(
            "SELECT year, days FROM version_usage WHERE version_id=?",
            (version_id,),
        ).fetchall()
        return {row["year"]: row["days"] for row in rows}

    def _live_versions(self, request_id):
        """(effective_row, pending_row) for a request."""
        rows = self._conn.execute(
            "SELECT * FROM versions WHERE request_id=? AND status IN"
            " ('effective','pending')",
            (request_id,),
        ).fetchall()
        effective = None
        pending = None
        for row in rows:
            if row["status"] == "effective":
                effective = row
            else:
                pending = row
        return effective, pending

    def _rebuild_occupancy(self, request_id, employee_id):
        """Occupied dates = union of effective and pending version dates."""
        self._conn.execute(
            "DELETE FROM occupancy WHERE request_id=?", (request_id,)
        )
        effective, pending = self._live_versions(request_id)
        days: set[str] = set()
        for row in (effective, pending):
            if row is not None:
                days.update(_dates.occupied_dates(
                    _dates.parse_date(row["start_date"]),
                    _dates.parse_date(row["end_date"]),
                ))
        self._conn.executemany(
            "INSERT INTO occupancy (employee_id, date, request_id)"
            " VALUES (?,?,?)",
            [(employee_id, d, request_id) for d in sorted(days)],
        )

    def _check_date_overlap(self, employee_id, request_id, start, end):
        """New dates must not touch other requests' occupied dates."""
        wanted = _dates.occupied_dates(start, end)
        if not wanted:
            return
        marks = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"SELECT date, request_id FROM occupancy WHERE employee_id=?"
            f" AND date IN ({marks}) AND request_id != ? LIMIT 1",
            [employee_id, *wanted, request_id],
        ).fetchall()
        if rows:
            raise DateOverlapError(
                f"date {rows[0]['date']} already occupied by request"
                f" {rows[0]['request_id']}"
            )

    def _contributions(self, employee_id):
        """Per request: {(year, leave_type): [effective_days, pending_days]}"""
        rows = self._conn.execute(
            "SELECT v.request_id, v.status, v.leave_type, u.year, u.days"
            " FROM versions v"
            " JOIN requests r ON r.request_id = v.request_id"
            " JOIN version_usage u ON u.version_id = v.version_id"
            " WHERE r.employee_id=? AND v.status IN ('effective','pending')",
            (employee_id,),
        ).fetchall()
        contrib: dict[str, dict[tuple[int, str], list[int]]] = {}
        for row in rows:
            per_req = contrib.setdefault(row["request_id"], {})
            slot = per_req.setdefault(
                (row["year"], row["leave_type"]), [0, 0]
            )
            if row["status"] == "effective":
                slot[0] += row["days"]
            else:
                slot[1] += row["days"]
        return contrib

    def _check_quota(self, employee_id, request_id, new_pending_usage,
                     new_pending_type):
        """Ensure sum of max(effective, pending) per request fits quota."""
        if new_pending_type == "unpaid":
            return
        contrib = self._contributions(employee_id)
        effective, _ = self._live_versions(request_id) if request_id else (None, None)
        # Recompute this request's contribution with the new pending version.
        per_req: dict[tuple[int, str], list[int]] = {}
        if effective is not None:
            for year, days in self._version_usage(
                effective["version_id"]
            ).items():
                per_req.setdefault((year, effective["leave_type"]), [0, 0])[0] = days
        for year, days in new_pending_usage.items():
            per_req.setdefault((year, new_pending_type), [0, 0])[1] = days
        contrib[request_id] = per_req
        # Keys that must be checked: anything this request touches.
        touched = set(per_req.keys())
        totals: dict[tuple[int, str], int] = {}
        for req_contrib in contrib.values():
            for key, (eff, pen) in req_contrib.items():
                totals[key] = totals.get(key, 0) + max(eff, pen)
        for (year, leave_type), total in sorted(totals.items()):
            if leave_type == "unpaid":
                continue
            if (year, leave_type) not in touched:
                continue
            quota = YEARLY_QUOTA[leave_type]
            if total > quota:
                raise QuotaExceededError(
                    f"{leave_type} quota exceeded for {year}:"
                    f" {total} > {quota}"
                )

    def submit_request(
        self,
        employee_id: str,
        leave_type: str,
        start,
        end,
        submission_id: Optional[str] = None,
    ) -> dict:
        """Submit a brand-new leave request (pending version)."""
        start = _dates.parse_date(start)
        end = _dates.parse_date(end)
        self._validate(leave_type, start, end)
        submission_id = submission_id or uuid.uuid4().hex
        request_id = uuid.uuid4().hex
        with self._tx():
            self._check_date_overlap(employee_id, request_id, start, end)
            usage = _dates.chargeable_days_by_year(start, end)
            self._check_quota(employee_id, None, usage, leave_type)
            self._conn.execute(
                "INSERT INTO requests (request_id, employee_id) VALUES (?,?)",
                (request_id, employee_id),
            )
            try:
                version_id = self._insert_version(
                    request_id, leave_type, start, end, None, submission_id
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"submission_id {submission_id} already used"
                ) from exc
            self._rebuild_occupancy(request_id, employee_id)
        return {
            "request_id": request_id,
            "version_id": version_id,
            "submission_id": submission_id,
        }

    def submit_change(
        self,
        request_id: str,
        base_version_id: str,
        leave_type: str,
        start,
        end,
        submission_id: Optional[str] = None,
    ) -> dict:
        """Submit a change against an existing effective version."""
        start = _dates.parse_date(start)
        end = _dates.parse_date(end)
        self._validate(leave_type, start, end)
        submission_id = submission_id or uuid.uuid4().hex
        with self._tx():
            row = self._conn.execute(
                "SELECT employee_id FROM requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"request {request_id} not found")
            employee_id = row["employee_id"]
            effective, pending = self._live_versions(request_id)
            if effective is None:
                raise ConflictError(
                    f"request {request_id} has no effective version"
                )
            if pending is not None:
                raise ConflictError(
                    f"request {request_id} already has a pending version"
                )
            if effective["version_id"] != base_version_id:
                raise ConflictError(
                    f"base version {base_version_id} is not the current"
                    f" effective version {effective['version_id']}"
                )
            self._check_date_overlap(employee_id, request_id, start, end)
            usage = _dates.chargeable_days_by_year(start, end)
            self._check_quota(employee_id, request_id, usage, leave_type)
            try:
                version_id = self._insert_version(
                    request_id, leave_type, start, end,
                    base_version_id, submission_id,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"submission_id {submission_id} already used"
                ) from exc
            self._rebuild_occupancy(request_id, employee_id)
        return {
            "request_id": request_id,
            "version_id": version_id,
            "submission_id": submission_id,
        }

    @staticmethod
    def _validate(leave_type, start, end):
        if leave_type not in LEAVE_TYPES:
            raise ValueError(f"unknown leave_type: {leave_type!r}")
        if end < start:
            raise ValueError(f"end {end} before start {start}")

    def approve(self, submission_id: str) -> dict:
        """Approve a pending submission; idempotent per submission."""
        return self._decide(submission_id, "approved")

    def reject(self, submission_id: str) -> dict:
        """Reject a pending submission; idempotent per submission."""
        return self._decide(submission_id, "rejected")

    def _decide(self, submission_id: str, verdict: str) -> dict:
        with self._tx():
            row = self._conn.execute(
                "SELECT verdict FROM approvals WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if row is not None:
                if row["verdict"] == verdict:
                    return {"submission_id": submission_id,
                            "verdict": verdict, "replayed": True}
                raise ConflictError(
                    f"submission {submission_id} already {row['verdict']}"
                )
            version = self._conn.execute(
                "SELECT * FROM versions WHERE submission_id=?",
                (submission_id,),
            ).fetchone()
            if version is None:
                raise NotFoundError(f"submission {submission_id} not found")
            if version["status"] != "pending":
                raise ConflictError(
                    f"submission {submission_id} is no longer pending"
                )
            if verdict == "approved":
                new_status = "effective"
                if version["base_version_id"] is not None:
                    self._conn.execute(
                        "UPDATE versions SET status='superseded'"
                        " WHERE version_id=?",
                        (version["base_version_id"],),
                    )
            else:
                new_status = "rejected"
            self._conn.execute(
                "UPDATE versions SET status=? WHERE version_id=?",
                (new_status, version["version_id"]),
            )
            self._conn.execute(
                "INSERT INTO approvals (submission_id, verdict)"
                " VALUES (?,?)",
                (submission_id, verdict),
            )
            employee_id = self._conn.execute(
                "SELECT employee_id FROM requests WHERE request_id=?",
                (version["request_id"],),
            ).fetchone()["employee_id"]
            self._rebuild_occupancy(version["request_id"], employee_id)
        return {"submission_id": submission_id, "verdict": verdict,
                "replayed": False}

    def get_request(self, request_id: str) -> dict:
        """Show effective + pending versions of one request."""
        row = self._conn.execute(
            "SELECT employee_id FROM requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"request {request_id} not found")
        effective, pending = self._live_versions(request_id)
        occupied = [
            r["date"]
            for r in self._conn.execute(
                "SELECT date FROM occupancy WHERE request_id=?"
                " ORDER BY date",
                (request_id,),
            )
        ]
        return {
            "request_id": request_id,
            "employee_id": row["employee_id"],
            "effective": self._version_dict(effective),
            "pending": self._version_dict(pending),
            "occupied_dates": occupied,
        }

    def _version_dict(self, row) -> Optional[dict]:
        if row is None:
            return None
        return {
            "version_id": row["version_id"],
            "leave_type": row["leave_type"],
            "start_date": row["start_date"],
            "end_date": row["end_date"],
            "status": row["status"],
            "submission_id": row["submission_id"],
            "base_version_id": row["base_version_id"],
            "usage": self._version_usage(row["version_id"]),
        }

    def get_balance(self, employee_id: str, year: int) -> dict:
        """Per leave-type used / extra-reserved / available for a year."""
        contrib = self._contributions(employee_id)
        used: dict[str, int] = {}
        reserved: dict[str, int] = {}
        for per_req in contrib.values():
            for (yr, leave_type), (eff, pen) in per_req.items():
                if yr != year:
                    continue
                used[leave_type] = used.get(leave_type, 0) + eff
                reserved[leave_type] = (
                    reserved.get(leave_type, 0) + max(eff, pen) - eff
                )
        types = {}
        for leave_type in LEAVE_TYPES:
            quota = YEARLY_QUOTA[leave_type]
            used_days = used.get(leave_type, 0)
            reserved_days = reserved.get(leave_type, 0)
            types[leave_type] = {
                "quota": quota,
                "used": used_days,
                "reserved": reserved_days,
                "available": (
                    None if quota is None
                    else quota - used_days - reserved_days
                ),
            }
        return {"employee_id": employee_id, "year": year, "types": types}

    def close(self) -> None:
        self._conn.close()
