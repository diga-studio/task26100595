"""争抢场景: 额度与日期占用在并发下只允许符合逐个执行语义的申请成功。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import date, timedelta

from peopledesk import (
    DateConflictError,
    LeaveType,
    PeopleDesk,
    QuotaExceededError,
)


class ContentionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)

    def tearDown(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.db_path + suffix):
                os.remove(self.db_path + suffix)


class TestQuotaRace(ContentionTestCase):
    def test_last_day_quota_race(self) -> None:
        """多个申请争抢最后 1 天额度, 只有一个成功。"""
        pd = PeopleDesk(self.db_path)
        req = pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 22))
        pd.approve(req.pending.submission_id)
        annual = next(b for b in pd.get_balance("E1", 2026).balances
                      if b.leave_type is LeaveType.ANNUAL)
        self.assertEqual(annual.available, 1)
        pd.close()
        results: list[str] = []
        lock = threading.Lock()

        def attempt(day: date) -> None:
            desk = PeopleDesk(self.db_path)
            try:
                desk.submit_new("E1", LeaveType.ANNUAL, day, day)
                outcome = "ok"
            except QuotaExceededError:
                outcome = "quota"
            finally:
                desk.close()
            with lock:
                results.append(outcome)

        days = [date(2026, 2, 2) + timedelta(days=i) for i in range(5)]
        threads = [threading.Thread(target=attempt, args=(d,)) for d in days]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1, "只有 1 个申请能拿到最后 1 天")
        self.assertEqual(results.count("quota"), 4)
        desk = PeopleDesk(self.db_path)
        annual = next(b for b in desk.get_balance("E1", 2026).balances
                      if b.leave_type is LeaveType.ANNUAL)
        self.assertEqual((annual.used, annual.extra_reserved, annual.available), (14, 1, 0))
        desk.close()


class TestDateOccupancy(ContentionTestCase):
    def test_date_race_same_day(self) -> None:
        """多个申请争抢同一日期, 只有一个成功。"""
        results: list[str] = []
        lock = threading.Lock()

        def attempt() -> None:
            desk = PeopleDesk(self.db_path)
            try:
                desk.submit_new("E1", LeaveType.ANNUAL, date(2026, 3, 2), date(2026, 3, 3))
                outcome = "ok"
            except DateConflictError:
                outcome = "conflict"
            finally:
                desk.close()
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("conflict"), 7)

    def test_overlap_rejected_despite_quota(self) -> None:
        """余额充足但日期相交也不能通过。"""
        pd = PeopleDesk(self.db_path)
        pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 4, 6), date(2026, 4, 10))
        with self.assertRaises(DateConflictError):
            pd.submit_new("E1", LeaveType.SICK, date(2026, 4, 10), date(2026, 4, 14))
        pd.close()

    def test_unpaid_and_weekend_occupy_dates(self) -> None:
        """无薪假与周末同样遵守占用规则。"""
        pd = PeopleDesk(self.db_path)
        pd.submit_new("E1", LeaveType.UNPAID, date(2026, 5, 1), date(2026, 5, 3))
        with self.assertRaises(DateConflictError):
            pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 5, 2), date(2026, 5, 2))
        unpaid = next(b for b in pd.get_balance("E1", 2026).balances
                      if b.leave_type is LeaveType.UNPAID)
        self.assertEqual((unpaid.used, unpaid.extra_reserved), (0, 1),
                         "无薪假待审期间展示预占用量(仅 05-01 为工作日)")
        self.assertIsNone(unpaid.available)
        pd.close()

    def test_unpaid_never_quota_exceeded(self) -> None:
        """无薪假不限额, 超长区间也能提交。"""
        pd = PeopleDesk(self.db_path)
        req = pd.submit_new("E1", LeaveType.UNPAID, date(2026, 1, 1), date(2026, 12, 31))
        self.assertIsNotNone(req.pending)
        pd.close()

    def test_change_may_overlap_own_dates(self) -> None:
        """变更可以与本单原日期重叠。"""
        pd = PeopleDesk(self.db_path)
        req = pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 6, 1), date(2026, 6, 5))
        pd.approve(req.pending.submission_id)
        base_id = pd.get_request(req.request_id).effective.version_id
        changed = pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 6, 3), date(2026, 6, 9)
        )
        self.assertIsNotNone(changed.pending)
        pd.close()


class TestConcurrentMixed(ContentionTestCase):
    def test_sixteen_threads_serializable(self) -> None:
        """16 个并发调用, 结果等价于某个逐个执行顺序。"""
        employees = [f"E{i:03d}" for i in range(200)]
        errors: list[BaseException] = []
        lock = threading.Lock()

        def submit(employee_id: str, day: date) -> None:
            desk = PeopleDesk(self.db_path)
            try:
                desk.submit_new(employee_id, LeaveType.ANNUAL, day, day)
            except (QuotaExceededError, DateConflictError):
                pass
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            finally:
                desk.close()

        work = [(emp, date(2026, 7, 6)) for emp in employees]
        threads = []
        chunk = (len(work) + 15) // 16
        for i in range(0, len(work), chunk):
            part = work[i:i + chunk]
            threads.append(threading.Thread(
                target=lambda p=part: [submit(e, d) for e, d in p]))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        desk = PeopleDesk(self.db_path)
        count = sum(len(desk.list_requests(emp)) for emp in employees)
        self.assertEqual(count, 200, "每名员工恰好一张单")
        desk.close()


if __name__ == "__main__":
    unittest.main()
