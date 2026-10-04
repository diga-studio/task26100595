"""审批场景: 结论唯一且不可变、旧送审编号重放、跨重启一致性、提交原子性。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date

from peopledesk import (
    DecisionConflictError,
    LeaveType,
    NotFoundError,
    PendingExistsError,
    PeopleDesk,
    QuotaExceededError,
    StaleBaseVersionError,
)


class ApprovalTestCase(unittest.TestCase):
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

    def annual(self, employee_id: str, year: int):
        return next(b for b in self.pd.get_balance(employee_id, year).balances
                    if b.leave_type is LeaveType.ANNUAL)


class TestDecisionImmutability(ApprovalTestCase):
    def test_approve_reject_race_single_outcome(self) -> None:
        """批准与拒绝竞争时只能产生一个最终结论。"""
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
        submission_id = req.pending.submission_id
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def decide(action: str) -> None:
            desk = PeopleDesk(self.db_path)
            try:
                if action == "approve":
                    result = desk.approve(submission_id)
                else:
                    result = desk.reject(submission_id)
                with lock:
                    outcomes.append(result)
            except DecisionConflictError:
                pass
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            finally:
                desk.close()

        threads = [threading.Thread(target=decide, args=(a,))
                   for a in ("approve", "reject") * 8]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertTrue(outcomes, "至少一个调用胜出")
        self.assertEqual(set(outcomes), {outcomes[0]}, "所有成功调用结论一致")
        final = outcomes[0]
        if final == "approved":
            self.assertEqual(self.pd.approve(submission_id), final)
            with self.assertRaises(DecisionConflictError):
                self.pd.reject(submission_id)
        else:
            self.assertEqual(self.pd.reject(submission_id), final)
            with self.assertRaises(DecisionConflictError):
                self.pd.approve(submission_id)

    def test_old_submission_replay_keeps_new_pending(self) -> None:
        """再次变更后重放旧审批, 新待审版本与余额不变。"""
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
        old_submission = req.pending.submission_id
        self.pd.approve(old_submission)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 2, 2), date(2026, 2, 6)
        )
        new_submission = changed.pending.submission_id
        before = self.pd.get_request(req.request_id)
        balance_before = self.annual("E1", 2026)
        self.assertEqual(self.pd.approve(old_submission), "approved")
        with self.assertRaises(DecisionConflictError):
            self.pd.reject(old_submission)
        after = self.pd.get_request(req.request_id)
        self.assertEqual(after, before, "新待审版本与生效版本保持不变")
        self.assertEqual(after.pending.submission_id, new_submission)
        self.assertEqual(self.annual("E1", 2026), balance_before, "余额保持不变")

    def test_unknown_submission(self) -> None:
        with self.assertRaises(NotFoundError):
            self.pd.approve("SB99999999")


class TestChangeGuards(ApprovalTestCase):
    def test_stale_base_version_rejected(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 1, 12), date(2026, 1, 14)
        )
        self.pd.approve(changed.pending.submission_id)
        with self.assertRaises(StaleBaseVersionError):
            self.pd.submit_change(
                req.request_id, base_id, LeaveType.ANNUAL,
                date(2026, 3, 2), date(2026, 3, 3),
            )

    def test_single_pending_per_request(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 1, 12), date(2026, 1, 13)
        )
        with self.assertRaises(PendingExistsError):
            self.pd.submit_change(
                req.request_id, base_id, LeaveType.ANNUAL,
                date(2026, 2, 2), date(2026, 2, 3),
            )

    def test_failed_submit_is_atomic(self) -> None:
        """跨年区间任一年度超额即整体失败, 不留下待审版本或部分扣减。"""
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 20))
        self.pd.approve(req.pending.submission_id)
        with self.assertRaises(QuotaExceededError):
            self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 12, 28), date(2027, 1, 8))
        self.assertEqual(len(self.pd.list_requests("E1")), 1, "不产生新请假单")
        b2026 = self.annual("E1", 2026)
        b2027 = self.annual("E1", 2027)
        self.assertEqual((b2026.used, b2026.extra_reserved, b2026.available), (12, 0, 3))
        self.assertEqual((b2027.used, b2027.extra_reserved, b2027.available), (0, 0, 15))


class TestPersistence(ApprovalTestCase):
    def test_reopen_keeps_state(self) -> None:
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
        submission_id = req.pending.submission_id
        self.pd.approve(submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.SICK, date(2026, 3, 2), date(2026, 3, 4)
        )
        new_submission = changed.pending.submission_id
        self.pd.close()
        self.pd = PeopleDesk(self.db_path)
        info = self.pd.get_request(req.request_id)
        self.assertEqual(info.effective.leave_type, LeaveType.ANNUAL)
        self.assertEqual(info.pending.submission_id, new_submission)
        self.assertEqual(self.pd.approve(submission_id), "approved", "旧结论跨重启保留")
        annual = self.annual("E1", 2026)
        self.assertEqual((annual.used, annual.extra_reserved), (5, 0))
        sick = next(b for b in self.pd.get_balance("E1", 2026).balances
                    if b.leave_type is LeaveType.SICK)
        self.assertEqual((sick.used, sick.extra_reserved), (0, 3))
        from peopledesk import DateConflictError
        with self.assertRaises(DateConflictError):
            self.pd.submit_new("E1", LeaveType.PERSONAL, date(2026, 3, 3), date(2026, 3, 3))

    def test_no_mid_transaction_visibility(self) -> None:
        """查询不得看到版本已切换而额度未更新的中间状态。"""
        req = self.pd.submit_new("E1", LeaveType.ANNUAL, date(2026, 1, 5), date(2026, 1, 9))
        self.pd.approve(req.pending.submission_id)
        base_id = self.pd.get_request(req.request_id).effective.version_id
        changed = self.pd.submit_change(
            req.request_id, base_id, LeaveType.ANNUAL, date(2026, 1, 12), date(2026, 1, 16)
        )
        self.pd.approve(changed.pending.submission_id)
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM versions GROUP BY status"
        ).fetchall()
        conn.close()
        states = dict(rows)
        self.assertEqual(states.get("effective"), 1)
        self.assertEqual(states.get("superseded"), 1)
        annual = self.annual("E1", 2026)
        self.assertEqual((annual.used, annual.extra_reserved), (5, 0))


if __name__ == "__main__":
    unittest.main()
