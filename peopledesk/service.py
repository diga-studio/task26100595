"""PeopleDesk Lite 业务接口。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date

from .dates import chargeable_days_by_year, daterange_days
from .db import connect, next_id
from .types import (
    ANNUAL_QUOTAS,
    Decision,
    DecisionConflictError,
    DateConflictError,
    LeaveType,
    NotFoundError,
    PendingExistsError,
    PeopleDeskStateError,
    QuotaExceededError,
    RequestInfo,
    StaleBaseVersionError,
    TypeBalance,
    VersionInfo,
    VersionStatus,
    YearBalance,
)


def _iso(day: date) -> str:
    return day.isoformat()


def _parse_day(text: str) -> date:
    return date.fromisoformat(text)


def _row_to_version(row: sqlite3.Row) -> VersionInfo:
    return VersionInfo(
        version_id=row["version_id"],
        submission_id=row["submission_id"],
        leave_type=LeaveType(row["leave_type"]),
        start_date=_parse_day(row["start_date"]),
        end_date=_parse_day(row["end_date"]),
        status=VersionStatus(row["status"]),
        base_version_id=row["base_version_id"],
    )


class PeopleDesk:
    """请假核算库入口, 全部状态保存在单个 SQLite 文件中。"""

    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "PeopleDesk":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self):
        """写事务: BEGIN IMMEDIATE 保证同一文件上的库实例逐个执行。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # 内部: 额度核算
    # ------------------------------------------------------------------

    @staticmethod
    def _usage_by_year(leave_type: LeaveType, start: date, end: date) -> dict[int, int]:
        """单个版本在各年度的计费用量。

        无薪假同样统计用量用于展示, 但额度为 None 不参与超额校验。
        """
        return chargeable_days_by_year(start, end)

    def _quota_totals(self, employee_id: str) -> dict[tuple[int, LeaveType], tuple[int, int]]:
        """返回 {(year, leave_type): (已用, 占用)}。

        已用 = 生效版本用量之和;
        占用 = 每张单 max(生效版本用量, 待审版本用量) 之和。
        """
        rows = self._conn.execute(
            "SELECT v.request_id, v.leave_type, v.start_date, v.end_date, v.status"
            " FROM versions v JOIN requests r ON r.request_id = v.request_id"
            " WHERE r.employee_id = ? AND v.status IN (?, ?)",
            (employee_id, VersionStatus.EFFECTIVE.value, VersionStatus.PENDING.value),
        ).fetchall()
        per_request: dict[str, dict[tuple[int, LeaveType], list[int]]] = {}
        for row in rows:
            leave_type = LeaveType(row["leave_type"])
            usage = self._usage_by_year(
                leave_type, _parse_day(row["start_date"]), _parse_day(row["end_date"])
            )
            buckets = per_request.setdefault(row["request_id"], {})
            for year, days in usage.items():
                slot = buckets.setdefault((year, leave_type), [0, 0])
                index = 0 if row["status"] == VersionStatus.EFFECTIVE.value else 1
                slot[index] += days
        totals: dict[tuple[int, LeaveType], tuple[int, int]] = {}
        for buckets in per_request.values():
            for key, (eff, pend) in buckets.items():
                used, reserved = totals.get(key, (0, 0))
                totals[key] = (used + eff, reserved + max(eff, pend))
        return totals

    def _check_quota(self, employee_id: str) -> None:
        """在当前事务内校验该员工全部年度/假种的占用不超过额度。"""
        for (year, leave_type), (used, reserved) in self._quota_totals(employee_id).items():
            quota = ANNUAL_QUOTAS[leave_type]
            if quota is not None and reserved > quota:
                raise QuotaExceededError(
                    f"员工 {employee_id} {year} 年 {leave_type.value} "
                    f"占用 {reserved} 天, 超过额度 {quota} 天"
                )

    # ------------------------------------------------------------------
    # 内部: 日期占用
    # ------------------------------------------------------------------

    def _add_occupancy(self, employee_id: str, request_id: str, start: date, end: date) -> None:
        """把区间自然日并入该单占用; 与其他单相交则报错。"""
        for day in daterange_days(start, end):
            row = self._conn.execute(
                "SELECT request_id FROM occupancy WHERE employee_id = ? AND day = ?",
                (employee_id, _iso(day)),
            ).fetchone()
            if row is not None and row["request_id"] != request_id:
                raise DateConflictError(
                    f"日期 {_iso(day)} 已被请假单 {row['request_id']} 占用"
                )
            self._conn.execute(
                "INSERT OR IGNORE INTO occupancy(employee_id, day, request_id)"
                " VALUES(?, ?, ?)",
                (employee_id, _iso(day), request_id),
            )

    def _refresh_occupancy(self, request_id: str) -> None:
        """按当前生效与待审版本的日期并集重建该单占用。"""
        row = self._conn.execute(
            "SELECT employee_id FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        employee_id = row["employee_id"]
        self._conn.execute("DELETE FROM occupancy WHERE request_id = ?", (request_id,))
        rows = self._conn.execute(
            "SELECT start_date, end_date FROM versions"
            " WHERE request_id = ? AND status IN (?, ?)",
            (request_id, VersionStatus.EFFECTIVE.value, VersionStatus.PENDING.value),
        ).fetchall()
        for version in rows:
            for day in daterange_days(
                _parse_day(version["start_date"]), _parse_day(version["end_date"])
            ):
                self._conn.execute(
                    "INSERT OR IGNORE INTO occupancy(employee_id, day, request_id)"
                    " VALUES(?, ?, ?)",
                    (employee_id, _iso(day), request_id),
                )

    # ------------------------------------------------------------------
    # 内部: 版本查询
    # ------------------------------------------------------------------

    def _get_version_by_status(self, request_id: str, status: VersionStatus) -> VersionInfo | None:
        row = self._conn.execute(
            "SELECT * FROM versions WHERE request_id = ? AND status = ?",
            (request_id, status.value),
        ).fetchone()
        return _row_to_version(row) if row else None

    def _load_request(self, request_id: str) -> RequestInfo:
        row = self._conn.execute(
            "SELECT employee_id FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"请假单不存在: {request_id}")
        return RequestInfo(
            request_id=request_id,
            employee_id=row["employee_id"],
            effective=self._get_version_by_status(request_id, VersionStatus.EFFECTIVE),
            pending=self._get_version_by_status(request_id, VersionStatus.PENDING),
        )

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def submit_new(
        self,
        employee_id: str,
        leave_type: LeaveType,
        start: date,
        end: date,
    ) -> RequestInfo:
        """提交新请假, 立即预占日期与额度。"""
        leave_type = LeaveType(leave_type)
        if end < start:
            raise ValueError("end 必须不早于 start")
        with self._tx():
            request_id = next_id(self._conn, "request", "RQ")
            submission_id = next_id(self._conn, "submission", "SB")
            self._conn.execute(
                "INSERT INTO requests(request_id, employee_id) VALUES(?, ?)",
                (request_id, employee_id),
            )
            self._conn.execute(
                "INSERT INTO versions(request_id, submission_id, leave_type,"
                " start_date, end_date, status, base_version_id)"
                " VALUES(?, ?, ?, ?, ?, ?, NULL)",
                (
                    request_id,
                    submission_id,
                    leave_type.value,
                    _iso(start),
                    _iso(end),
                    VersionStatus.PENDING.value,
                ),
            )
            self._add_occupancy(employee_id, request_id, start, end)
            self._check_quota(employee_id)
            return self._load_request(request_id)

    def submit_change(
        self,
        request_id: str,
        base_version_id: int,
        leave_type: LeaveType,
        start: date,
        end: date,
    ) -> RequestInfo:
        """基于指定生效版本提交变更, 送审期间原版本仍生效。"""
        leave_type = LeaveType(leave_type)
        if end < start:
            raise ValueError("end 必须不早于 start")
        with self._tx():
            info = self._load_request(request_id)
            if info.effective is None:
                raise NotFoundError(f"请假单 {request_id} 没有生效版本")
            if info.effective.version_id != base_version_id:
                raise StaleBaseVersionError(
                    f"依据版本 {base_version_id} 已过期,"
                    f"当前生效版本为 {info.effective.version_id}"
                )
            if info.pending is not None:
                raise PendingExistsError(f"请假单 {request_id} 已存在待审版本")
            submission_id = next_id(self._conn, "submission", "SB")
            self._conn.execute(
                "INSERT INTO versions(request_id, submission_id, leave_type,"
                " start_date, end_date, status, base_version_id)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    submission_id,
                    leave_type.value,
                    _iso(start),
                    _iso(end),
                    VersionStatus.PENDING.value,
                    base_version_id,
                ),
            )
            self._add_occupancy(info.employee_id, request_id, start, end)
            self._check_quota(info.employee_id)
            return self._load_request(request_id)

    # ------------------------------------------------------------------
    # 审批
    # ------------------------------------------------------------------

    def _decide(self, submission_id: str, decision: Decision) -> str:
        with self._tx():
            row = self._conn.execute(
                "SELECT decision FROM approvals WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            if row is not None:
                if row["decision"] == decision.value:
                    return row["decision"]  # 重复相同结论, 返回原结论
                raise DecisionConflictError(
                    f"送审 {submission_id} 已{row['decision']}, 不能再{decision.value}"
                )
            version = self._conn.execute(
                "SELECT * FROM versions WHERE submission_id = ?", (submission_id,)
            ).fetchone()
            if version is None:
                raise NotFoundError(f"送审编号不存在: {submission_id}")
            if version["status"] != VersionStatus.PENDING.value:
                # 防御: 已决定的送审必然已有 approvals 记录
                raise PeopleDeskStateError(f"送审 {submission_id} 状态异常")
            request_id = version["request_id"]
            if decision is Decision.APPROVED:
                self._conn.execute(
                    "UPDATE versions SET status = ? WHERE version_id = ?",
                    (VersionStatus.EFFECTIVE.value, version["version_id"]),
                )
                if version["base_version_id"] is not None:
                    self._conn.execute(
                        "UPDATE versions SET status = ? WHERE version_id = ?",
                        (VersionStatus.SUPERSEDED.value, version["base_version_id"]),
                    )
            else:
                self._conn.execute(
                    "UPDATE versions SET status = ? WHERE version_id = ?",
                    (VersionStatus.REJECTED.value, version["version_id"]),
                )
            self._conn.execute(
                "INSERT INTO approvals(submission_id, decision) VALUES(?, ?)",
                (submission_id, decision.value),
            )
            self._refresh_occupancy(request_id)
            return decision.value

    def approve(self, submission_id: str) -> str:
        """批准一次送审, 返回最终结论。幂等。"""
        return self._decide(submission_id, Decision.APPROVED)

    def reject(self, submission_id: str) -> str:
        """拒绝一次送审, 返回最终结论。幂等。"""
        return self._decide(submission_id, Decision.REJECTED)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_request(self, request_id: str) -> RequestInfo:
        """查询一张单的生效版本与待审版本。"""
        with self._lock:
            return self._load_request(request_id)

    def list_requests(self, employee_id: str) -> list[RequestInfo]:
        """列出员工全部请假单。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT request_id FROM requests WHERE employee_id = ?"
                " ORDER BY request_id",
                (employee_id,),
            ).fetchall()
            return [self._load_request(row["request_id"]) for row in rows]

    def get_balance(self, employee_id: str, year: int) -> YearBalance:
        """查询某员工某年度各假种的已用 / 额外预占 / 可用余额。"""
        with self._lock:
            totals = self._quota_totals(employee_id)
            balances = []
            for leave_type in LeaveType:
                used, reserved = totals.get((year, leave_type), (0, 0))
                quota = ANNUAL_QUOTAS[leave_type]
                balances.append(
                    TypeBalance(
                        leave_type=leave_type,
                        quota=quota,
                        used=used,
                        extra_reserved=reserved - used,
                        available=None if quota is None else quota - reserved,
                    )
                )
            return YearBalance(
                employee_id=employee_id, year=year, balances=tuple(balances)
            )
