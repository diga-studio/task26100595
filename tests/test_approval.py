"""Approval verdicts: final, idempotent, and safe against stale replays."""

import pytest

from peopledesk import ConflictError, NotFoundError


def _approved_request(desk, submission_id="sub-new"):
    result = desk.submit_request("E1", "annual", "2025-01-06", "2025-01-17",
                                 submission_id=submission_id)
    desk.approve(submission_id)
    return result


def test_same_verdict_replay_returns_original(desk):
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                        submission_id="sub-x")
    first = desk.approve("sub-x")
    assert first == {"submission_id": "sub-x", "verdict": "approved",
                     "replayed": False}
    again = desk.approve("sub-x")
    assert again["verdict"] == "approved"
    assert again["replayed"] is True


def test_opposite_verdict_conflicts(desk):
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                        submission_id="sub-x")
    desk.approve("sub-x")
    with pytest.raises(ConflictError):
        desk.reject("sub-x")

    desk.submit_request("E1", "annual", "2025-02-03", "2025-02-05",
                        submission_id="sub-y")
    desk.reject("sub-y")
    with pytest.raises(ConflictError):
        desk.approve("sub-y")
    # Balance untouched by the failed flips.
    balance = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (balance["used"], balance["reserved"]) == (5, 0)


def test_approve_reject_race_single_winner(desk):
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                        submission_id="sub-race")
    desk.approve("sub-race")
    with pytest.raises(ConflictError):
        desk.reject("sub-race")
    request = desk.get_request(
        desk._conn.execute("SELECT request_id FROM requests").fetchone()[0])
    assert request["effective"] is not None
    assert request["pending"] is None


def test_unknown_submission(desk):
    with pytest.raises(NotFoundError):
        desk.approve("no-such-submission")
    with pytest.raises(NotFoundError):
        desk.reject("no-such-submission")


def test_stale_approval_replay_does_not_touch_new_pending(desk):
    result = _approved_request(desk)  # 10 days, approved via sub-new
    request = desk.get_request(result["request_id"])
    base_v1 = request["effective"]["version_id"]

    # Change 1: extend to 12 days, rejected.
    desk.submit_change(result["request_id"], base_v1, "annual",
                       "2025-01-06", "2025-01-21", submission_id="sub-chg1")
    desk.reject("sub-chg1")

    # Change 2: different 3-day segment in February, now pending.
    desk.submit_change(result["request_id"], base_v1, "annual",
                       "2025-02-03", "2025-02-05", submission_id="sub-chg2")
    before_request = desk.get_request(result["request_id"])
    before_balance = desk.get_balance("E1", 2025)

    # Replay the old decision on sub-chg1: must be a no-op returning the
    # original verdict, and must not affect the new pending version.
    replay = desk.reject("sub-chg1")
    assert replay["verdict"] == "rejected"
    assert replay["replayed"] is True
    with pytest.raises(ConflictError):
        desk.approve("sub-chg1")

    after_request = desk.get_request(result["request_id"])
    after_balance = desk.get_balance("E1", 2025)
    assert after_request == before_request
    assert after_balance == before_balance
    assert after_request["pending"]["submission_id"] == "sub-chg2"

    # The new pending version can still be decided normally.
    desk.approve("sub-chg2")
    final = desk.get_request(result["request_id"])
    assert final["effective"]["start_date"] == "2025-02-03"
    balance = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (balance["used"], balance["reserved"]) == (3, 0)


def test_approval_of_old_submission_not_applied_to_new_version(desk):
    result = _approved_request(desk)
    request = desk.get_request(result["request_id"])
    base_v1 = request["effective"]["version_id"]
    desk.submit_change(result["request_id"], base_v1, "annual",
                       "2025-01-06", "2025-01-21", submission_id="sub-old")
    desk.approve("sub-old")  # now effective: 12 days
    # New change based on the new effective version.
    request = desk.get_request(result["request_id"])
    base_v2 = request["effective"]["version_id"]
    desk.submit_change(result["request_id"], base_v2, "annual",
                       "2025-03-03", "2025-03-07", submission_id="sub-new2")
    before = desk.get_balance("E1", 2025)
    # Replaying approval of sub-old must not release/reserve anything.
    replay = desk.approve("sub-old")
    assert replay["replayed"] is True
    assert desk.get_balance("E1", 2025) == before
    pending = desk.get_request(result["request_id"])["pending"]
    assert pending["submission_id"] == "sub-new2"


def test_duplicate_submission_id_rejected(desk):
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                        submission_id="sub-dup")
    desk.approve("sub-dup")
    with pytest.raises(ConflictError):
        desk.submit_request("E1", "annual", "2025-02-03", "2025-02-05",
                            submission_id="sub-dup")
