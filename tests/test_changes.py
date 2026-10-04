"""变更场景: 同年度加天、跨年度、跨假种, 批准与拒绝后的日期占用与额度账目。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date

from peopledesk import (
    ANNUAL_QUOTAS,
    LeaveType,
    PeopleDesk,
    RequestInfo,
)
from peopledesk.dates import chargeable_days_by_year


def expected_totals(requests: list[RequestInfo]) -> dict[tuple[int, LeaveType], tuple[int, int]]:
    """按生效、待审版本独立计算 (已用, 占用), 作为 get_balance 的对照。"""
    totals: dict[tuple[int, LeaveType], tuple[int, int]] = {}
    for req in requests:
        buckets: dict[tuple[int, LeaveType], list[int]] = {}
        for version, index in ((req.effective, 0), (req.pending, 1)):
            if version is None or version.leave_type is LeaveType.UNPAID:
                continue
            usage = chargeable_days_by_year(version.start_date, version.end_date)
            for year, days in usage.items():
                slot = buckets.setdefault((year, version.leave_type), [0, 0])
                slot[index] += days
        for key, (eff, pend) in buckets.items():
            used, reserved = totals.get(key, (0, 0))
            totals[key] = (used + eff, reserved + max(eff, pend))
    return totals


class PeopleDeskTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.pd = PeopleDesk(self.db_path)

    def tearDown(self) -> None:
        self.pd.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.remove(self.db_path + suffix)

    def assert_balances_match_independent(self, employee_id: str, years: list[int]) -> None:
        totals = expected_totals(self.pd.list_requests(employee_id))
        for year in years:
            balance = self.pd.get_balance(employee_id, year)
            for entry in balance.balances:
                used, reserved = totals.get((year, entry.leave_type), (0, 0))
                self.assertEqual(entry.used, used, f"{year} {entry.leave_type} used")
                self.assertEqual(entry.extra_reserved, reserved - used,
                                 f"{year} {entry.leave_type} extra_reserved")
                quota = ANNUAL_QUOTAS[entry.leave_type]
                if quota is None:
                    self.assertIsNone(entry.available)
                else:
                    self.assertEqual(entry.available, quota - reserved,
                                     f"{year} {entry.leave_type} available")

    def balance_of(self, employee_id: str, year: int, leave_type: LeaveType):
        balance = self.pd.get_balance(employee_id, year)
        return next(b for b in balance.balances if b.leave_type is leave_type)


class TestSameYearChange(PeopleDeskTestCase):
    def test_extend_10_to_12_approve(self) -> None:
        # 10 天年假: 2026-01-05(周一) ~ 2026-01-16(周五)
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 16))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id

        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 20)
        )
        self.assertIsNotNone(changed.effective, "送审期间原版本仍生效")
        annual = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        self.assertEqual((annual.used, annual.extra_reserved, annual.available), (10, 2, 3))

        self.pd.approve(changed.pending.submission_id)
        after = self.pd.get_request(req.request_id)
        self.assertIsNone(after.pending)
        self.assertEqual(after.effective.end_date, date(2026, 1, 20))
        annual = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        self.assertEqual((annual.used, annual.extra_reserved, annual.available), (12, 0, 3))
        self.assert_balances_match_independent("E1", [2026])

    def test_extend_10_to_12_reject(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 16))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 20)
        )
        self.pd.reject(changed.pending.submission_id)
        after = self.pd.get_request(req.request_id)
        self.assertIsNone(after.pending)
        self.assertEqual(after.effective.end_date, date(2026, 1, 16), "拒绝后原版本不变")
        annual = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        self.assertEqual((annual.used, annual.extra_reserved, annual.available), (10, 0, 5))
        # 拒绝后新增日期(01-19/01-20)的占用被释放, 可再被其他单使用
        other = self.pd.submit_new("E1", LeaveType.PERSONAL, date(2026, 1, 19), date(2026, 1, 20))
        self.assertIsNotNone(other.pending)
        self.assert_balances_match_independent("E1", [2026])


class TestCrossYearChange(PeopleDeskTestCase):
    def test_move_to_next_year_approve(self) -> None:
        # 2026 年 5 天年假, 改到 2027 年 3 天
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 3, 2), date(2026, 3, 6))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2027, 1, 4), date(2027, 1, 6)
        )
        b2026 = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        b2027 = self.balance_of("E1", 2027, LeaveType.ANNUAL)
        self.assertEqual((b2026.used, b2026.extra_reserved), (5, 0), "原年度保留原用量")
        self.assertEqual((b2027.used, b2027.extra_reserved), (0, 3), "目标年度预占新用量")

        self.pd.approve(changed.pending.submission_id)
        b2026 = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        b2027 = self.balance_of("E1", 2027, LeaveType.ANNUAL)
        self.assertEqual((b2026.used, b2026.extra_reserved, b2026.available), (0, 0, 15))
        self.assertEqual((b2027.used, b2027.extra_reserved, b2027.available), (3, 0, 12))
        # 2026 年原日期占用已释放
        self.pd.submit_new("E1", LeaveType.PERSONAL, date(2026, 3, 2), date(2026, 3, 3))
        self.assert_balances_match_independent("E1", [2026, 2027])

    def test_move_to_next_year_reject(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 3, 2), date(2026, 3, 6))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2027, 1, 4), date(2027, 1, 6)
        )
        self.pd.reject(changed.pending.submission_id)
        b2026 = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        b2027 = self.balance_of("E1", 2027, LeaveType.ANNUAL)
        self.assertEqual((b2026.used, b2026.extra_reserved), (5, 0))
        self.assertEqual((b2027.used, b2027.extra_reserved), (0, 0))
        # 2027 年目标日期占用已释放, 2026 年原日期仍被占用
        self.pd.submit_new("E1", LeaveType.PERSONAL, date(2027, 1, 4), date(2027, 1, 5))
        from peopledesk import DateConflictError
        with self.assertRaises(DateConflictError):
            self.pd.submit_new("E1", LeaveType.PERSONAL, date(2026, 3, 6), date(2026, 3, 6))
        self.assert_balances_match_independent("E1", [2026, 2027])


class TestCrossTypeChange(PeopleDeskTestCase):
    def test_annual_to_sick_approve(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 2, 2), date(2026, 2, 6))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.SICK, date(2026, 2, 2), date(2026, 2, 4)
        )
        annual = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        sick = self.balance_of("E1", 2026, LeaveType.SICK)
        self.assertEqual((annual.used, annual.extra_reserved), (5, 0), "年假保留原用量")
        self.assertEqual((sick.used, sick.extra_reserved), (0, 3), "病假预占新用量")

        self.pd.approve(changed.pending.submission_id)
        annual = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        sick = self.balance_of("E1", 2026, LeaveType.SICK)
        self.assertEqual((annual.used, annual.available), (0, 15))
        self.assertEqual((sick.used, sick.available), (3, 7))
        self.assert_balances_match_independent("E1", [2026])

    def test_annual_to_sick_reject(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 2, 2), date(2026, 2, 6))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.SICK, date(2026, 2, 2), date(2026, 2, 4)
        )
        self.pd.reject(changed.pending.submission_id)
        annual = self.balance_of("E1", 2026, LeaveType.ANNUAL)
        sick = self.balance_of("E1", 2026, LeaveType.SICK)
        self.assertEqual((annual.used, annual.extra_reserved), (5, 0))
        self.assertEqual((sick.used, sick.extra_reserved), (0, 0))
        self.assert_balances_match_independent("E1", [2026])


if __name__ == "__main__":
    unittest.main()
