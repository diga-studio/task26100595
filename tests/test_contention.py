"""Contention: last quota day, overlapping dates, unpaid and weekends."""

import pytest

from peopledesk import DateOverlapError, QuotaExceededError


def test_last_quota_day_allows_exactly_one(desk):
    # Use up 14 of 15 annual days: 2025-01-06..2025-01-23 = 14 workdays.
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-23",
                        submission_id="sub-big")
    desk.approve("sub-big")
    assert desk.get_balance("E1", 2025)["types"]["annual"]["available"] == 1

    # Two competing 1-day requests (different dates, so only quota bites).
    desk.submit_request("E1", "annual", "2025-02-03", "2025-02-03",
                        submission_id="sub-winner")
    with pytest.raises(QuotaExceededError):
        desk.submit_request("E1", "annual", "2025-02-04", "2025-02-04",
                            submission_id="sub-loser")
    # Loser left nothing behind; rejecting the winner frees the day.
    desk.reject("sub-winner")
    assert desk.get_balance("E1", 2025)["types"]["annual"]["available"] == 1
    desk.submit_request("E1", "annual", "2025-02-04", "2025-02-04",
                        submission_id="sub-retry")
    assert desk.get_balance("E1", 2025)["types"]["annual"]["available"] == 0


def test_pending_reservation_blocks_competitor(desk):
    # 10 approved + one pending 5-day request => only 0 left for others.
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-17",
                        submission_id="sub-10")
    desk.approve("sub-10")
    desk.submit_request("E1", "annual", "2025-02-03", "2025-02-07",
                        submission_id="sub-5")
    assert desk.get_balance("E1", 2025)["types"]["annual"]["available"] == 0
    with pytest.raises(QuotaExceededError):
        desk.submit_request("E1", "annual", "2025-03-03", "2025-03-03",
                            submission_id="sub-extra")


def test_same_date_allows_exactly_one(desk):
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                        submission_id="sub-first")
    with pytest.raises(DateOverlapError):
        desk.submit_request("E1", "sick", "2025-01-10", "2025-01-14",
                            submission_id="sub-second")
    # After the first is rejected, its dates are free again.
    desk.reject("sub-first")
    desk.submit_request("E1", "sick", "2025-01-10", "2025-01-14",
                        submission_id="sub-second")


def test_overlap_checked_even_with_plenty_of_quota(desk):
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-08",
                        submission_id="sub-a")
    desk.approve("sub-a")
    assert desk.get_balance("E1", 2025)["types"]["annual"]["available"] == 12
    with pytest.raises(DateOverlapError):
        desk.submit_request("E1", "annual", "2025-01-08", "2025-01-10",
                            submission_id="sub-b")


def test_weekends_are_occupied_but_not_charged(desk):
    # 2025-01-11/12 is a weekend; request covers Fri..Mon.
    desk.submit_request("E1", "annual", "2025-01-10", "2025-01-13",
                        submission_id="sub-span")
    desk.approve("sub-span")
    request = desk.get_request(
        desk._conn.execute("SELECT request_id FROM requests").fetchone()[0])
    assert request["occupied_dates"] == [
        "2025-01-10", "2025-01-11", "2025-01-12", "2025-01-13"]
    assert request["effective"]["usage"] == {2025: 2}  # only Fri + Mon
    # A weekend-only request still collides on those dates.
    with pytest.raises(DateOverlapError):
        desk.submit_request("E1", "annual", "2025-01-11", "2025-01-12",
                            submission_id="sub-weekend")


def test_unpaid_leave_unlimited_but_occupies_dates(desk):
    desk.submit_request("E1", "unpaid", "2025-01-06", "2025-01-31",
                        submission_id="sub-unpaid")
    desk.approve("sub-unpaid")
    balance = desk.get_balance("E1", 2025)["types"]["unpaid"]
    assert balance["quota"] is None
    assert balance["available"] is None
    assert balance["used"] == 20  # 20 workdays in that range
    # Quota types are untouched.
    assert desk.get_balance("E1", 2025)["types"]["annual"]["available"] == 15
    # But dates are occupied: no other request may overlap, even unpaid.
    with pytest.raises(DateOverlapError):
        desk.submit_request("E1", "unpaid", "2025-01-15", "2025-01-15",
                            submission_id="sub-unpaid2")
    with pytest.raises(DateOverlapError):
        desk.submit_request("E1", "annual", "2025-01-31", "2025-02-04",
                            submission_id="sub-annual")


def test_unpaid_change_never_hits_quota(desk):
    desk.submit_request("E1", "unpaid", "2025-01-06", "2025-01-10",
                        submission_id="sub-u")
    desk.approve("sub-u")
    request = desk.get_request(
        desk._conn.execute("SELECT request_id FROM requests").fetchone()[0])
    # Extend to a huge range: no quota error for unpaid.
    desk.submit_change(request["request_id"],
                       request["effective"]["version_id"], "unpaid",
                       "2025-01-06", "2025-12-31", submission_id="sub-u2")
    pending = desk.get_request(request["request_id"])["pending"]
    assert pending["leave_type"] == "unpaid"
