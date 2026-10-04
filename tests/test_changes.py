"""Change flows: same-year, cross-year, cross-type; approve and reject."""

import pytest

from peopledesk import (
    ConflictError,
    DateOverlapError,
    QuotaExceededError,
)

# 2025-01-06 (Mon) .. 2025-01-19 (Sun): 10 workdays, 14 calendar days.
START_10 = "2025-01-06"
END_10 = "2025-01-19"
# 2025-01-06 .. 2025-01-21 (Tue): 12 workdays.
END_12 = "2025-01-21"


def _approved_10(desk, employee="E1", submission_id="sub-new"):
    result = desk.submit_request(employee, "annual", START_10, END_10,
                                 submission_id=submission_id)
    desk.approve(submission_id)
    return result


def _expected_balance(desk, employee, year):
    """Independently recompute accounts from effective/pending versions."""
    # Mirror of the spec: per request, max(effective, pending) per
    # (year, type); used = sum of effective; reserved = the extra part.
    rows = desk._conn.execute(
        "SELECT v.request_id, v.status, v.leave_type, u.year, u.days"
        " FROM versions v"
        " JOIN requests r ON r.request_id = v.request_id"
        " JOIN version_usage u ON u.version_id = v.version_id"
        " WHERE r.employee_id=? AND v.status IN ('effective','pending')",
        (employee,),
    ).fetchall()
    per_req = {}
    for request_id, status, leave_type, yr, days in rows:
        if yr != year:
            continue
        slot = per_req.setdefault(request_id, {}).setdefault(
            leave_type, [0, 0])
        slot[0 if status == "effective" else 1] += days
    used, reserved = {}, {}
    for per_type in per_req.values():
        for leave_type, (eff, pen) in per_type.items():
            used[leave_type] = used.get(leave_type, 0) + eff
            extra = max(eff, pen) - eff
            reserved[leave_type] = reserved.get(leave_type, 0) + extra
    return used, reserved


def _assert_balance_matches(desk, employee, year):
    balance = desk.get_balance(employee, year)["types"]
    used, reserved = _expected_balance(desk, employee, year)
    for leave_type in ("annual", "sick", "personal"):
        entry = balance[leave_type]
        assert entry["used"] == used.get(leave_type, 0)
        assert entry["reserved"] == reserved.get(leave_type, 0)
        assert entry["available"] == (
            entry["quota"] - entry["used"] - entry["reserved"]
        )


def test_same_year_extend_approve(desk):
    result = _approved_10(desk)
    request = desk.get_request(result["request_id"])
    base_version = request["effective"]["version_id"]

    desk.submit_change(result["request_id"], base_version, "annual",
                       START_10, END_12, submission_id="sub-chg")
    during = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (during["used"], during["reserved"], during["available"]) == (
        10, 2, 3)
    _assert_balance_matches(desk, "E1", 2025)

    # Original version still effective while the change is pending.
    request = desk.get_request(result["request_id"])
    assert request["effective"]["end_date"] == END_10
    assert request["pending"]["end_date"] == END_12
    # Occupancy is the union: 01-06 .. 01-21 inclusive (16 calendar days).
    assert request["occupied_dates"][0] == "2025-01-06"
    assert request["occupied_dates"][-1] == "2025-01-21"
    assert len(request["occupied_dates"]) == 16

    desk.approve("sub-chg")
    after = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (after["used"], after["reserved"], after["available"]) == (
        12, 0, 3)
    request = desk.get_request(result["request_id"])
    assert request["pending"] is None
    assert request["effective"]["end_date"] == END_12
    assert len(request["occupied_dates"]) == 16
    _assert_balance_matches(desk, "E1", 2025)


def test_same_year_extend_reject(desk):
    result = _approved_10(desk)
    request = desk.get_request(result["request_id"])
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "annual",
                       START_10, END_12, submission_id="sub-chg")
    desk.reject("sub-chg")
    after = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (after["used"], after["reserved"], after["available"]) == (
        10, 0, 5)
    request = desk.get_request(result["request_id"])
    assert request["pending"] is None
    assert request["effective"]["end_date"] == END_10
    assert request["occupied_dates"][-1] == "2025-01-19"
    assert len(request["occupied_dates"]) == 14
    _assert_balance_matches(desk, "E1", 2025)


def test_cross_year_change_approve(desk):
    # 2024-12-30 (Mon) .. 2025-01-10 (Fri): 2 days in 2024, 8 in 2025.
    result = desk.submit_request("E1", "annual", "2024-12-30", "2025-01-10",
                                 submission_id="sub-new")
    desk.approve("sub-new")
    request = desk.get_request(result["request_id"])
    assert request["effective"]["usage"] == {2024: 2, 2025: 8}

    # Move entirely into 2025: 2025-03-03 (Mon) .. 2025-03-14 (Fri) = 10.
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "annual",
                       "2025-03-03", "2025-03-14", submission_id="sub-chg")
    bal24 = desk.get_balance("E1", 2024)["types"]["annual"]
    bal25 = desk.get_balance("E1", 2025)["types"]["annual"]
    # 2024 keeps the effective 2 days. In 2025 the effective version
    # already uses 8 days, so the pending 10 only reserves 2 extra.
    assert (bal24["used"], bal24["reserved"]) == (2, 0)
    assert (bal25["used"], bal25["reserved"]) == (8, 2)

    desk.approve("sub-chg")
    bal24 = desk.get_balance("E1", 2024)["types"]["annual"]
    bal25 = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (bal24["used"], bal24["reserved"]) == (0, 0)
    assert (bal25["used"], bal25["reserved"]) == (10, 0)
    request = desk.get_request(result["request_id"])
    assert request["occupied_dates"][0] == "2025-03-03"
    assert request["occupied_dates"][-1] == "2025-03-14"
    _assert_balance_matches(desk, "E1", 2024)
    _assert_balance_matches(desk, "E1", 2025)


def test_cross_year_change_reject(desk):
    result = desk.submit_request("E1", "annual", "2024-12-30", "2025-01-10",
                                 submission_id="sub-new")
    desk.approve("sub-new")
    request = desk.get_request(result["request_id"])
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "annual",
                       "2025-03-03", "2025-03-14", submission_id="sub-chg")
    desk.reject("sub-chg")
    bal24 = desk.get_balance("E1", 2024)["types"]["annual"]
    bal25 = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (bal24["used"], bal24["reserved"]) == (2, 0)
    assert (bal25["used"], bal25["reserved"]) == (8, 0)
    request = desk.get_request(result["request_id"])
    assert request["occupied_dates"][0] == "2024-12-30"
    assert request["occupied_dates"][-1] == "2025-01-10"
    _assert_balance_matches(desk, "E1", 2024)
    _assert_balance_matches(desk, "E1", 2025)


def test_move_to_other_year_reserves_full_new_usage(desk):
    # Effective version entirely in 2024: 2024-12-23 (Mon) .. 2025-01-02
    # would straddle; use 2024-12-16 (Mon) .. 2024-12-27 (Fri) = 10 days.
    result = desk.submit_request("E1", "annual", "2024-12-16", "2024-12-27",
                                 submission_id="sub-new")
    desk.approve("sub-new")
    request = desk.get_request(result["request_id"])
    # Move to 2025-03-03 .. 2025-03-14 (10 workdays), entirely in 2025.
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "annual",
                       "2025-03-03", "2025-03-14", submission_id="sub-chg")
    bal24 = desk.get_balance("E1", 2024)["types"]["annual"]
    bal25 = desk.get_balance("E1", 2025)["types"]["annual"]
    # Original year keeps the old usage; target year reserves all 10.
    assert (bal24["used"], bal24["reserved"]) == (10, 0)
    assert (bal25["used"], bal25["reserved"]) == (0, 10)
    desk.approve("sub-chg")
    bal24 = desk.get_balance("E1", 2024)["types"]["annual"]
    bal25 = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (bal24["used"], bal24["reserved"]) == (0, 0)
    assert (bal25["used"], bal25["reserved"]) == (10, 0)
    _assert_balance_matches(desk, "E1", 2024)
    _assert_balance_matches(desk, "E1", 2025)


def test_cross_type_change_approve(desk):
    result = _approved_10(desk)  # annual, 10 days
    request = desk.get_request(result["request_id"])
    # Change to sick leave, same dates.
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "sick",
                       START_10, END_10, submission_id="sub-chg")
    annual = desk.get_balance("E1", 2025)["types"]["annual"]
    sick = desk.get_balance("E1", 2025)["types"]["sick"]
    # Annual keeps effective 10; sick reserves 10 on top.
    assert (annual["used"], annual["reserved"]) == (10, 0)
    assert (sick["used"], sick["reserved"]) == (0, 10)

    desk.approve("sub-chg")
    annual = desk.get_balance("E1", 2025)["types"]["annual"]
    sick = desk.get_balance("E1", 2025)["types"]["sick"]
    assert (annual["used"], annual["reserved"]) == (0, 0)
    assert (sick["used"], sick["reserved"]) == (10, 0)
    request = desk.get_request(result["request_id"])
    assert request["effective"]["leave_type"] == "sick"
    _assert_balance_matches(desk, "E1", 2025)


def test_cross_type_change_reject(desk):
    result = _approved_10(desk)
    request = desk.get_request(result["request_id"])
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "sick",
                       START_10, END_10, submission_id="sub-chg")
    desk.reject("sub-chg")
    annual = desk.get_balance("E1", 2025)["types"]["annual"]
    sick = desk.get_balance("E1", 2025)["types"]["sick"]
    assert (annual["used"], annual["reserved"]) == (10, 0)
    assert (sick["used"], sick["reserved"]) == (0, 0)
    _assert_balance_matches(desk, "E1", 2025)


def test_change_requires_current_base_version(desk):
    result = _approved_10(desk)
    request = desk.get_request(result["request_id"])
    stale_base = request["effective"]["version_id"]
    desk.submit_change(result["request_id"], stale_base, "annual",
                       START_10, END_12, submission_id="sub-chg1")
    desk.approve("sub-chg1")
    # stale_base is superseded now; using it must fail.
    with pytest.raises(ConflictError):
        desk.submit_change(result["request_id"], stale_base, "annual",
                           "2025-02-03", "2025-02-07",
                           submission_id="sub-chg2")


def test_only_one_pending_version(desk):
    result = _approved_10(desk)
    request = desk.get_request(result["request_id"])
    base = request["effective"]["version_id"]
    desk.submit_change(result["request_id"], base, "annual",
                       START_10, END_12, submission_id="sub-chg1")
    with pytest.raises(ConflictError):
        desk.submit_change(result["request_id"], base, "annual",
                           "2025-02-03", "2025-02-07",
                           submission_id="sub-chg2")


def test_failed_change_leaves_no_trace(desk):
    result = _approved_10(desk)
    request = desk.get_request(result["request_id"])
    base = request["effective"]["version_id"]
    # 20 extra workdays would blow the 15-day annual quota.
    with pytest.raises(QuotaExceededError):
        desk.submit_change(result["request_id"], base, "annual",
                           "2025-06-02", "2025-06-27",
                           submission_id="sub-big")
    request = desk.get_request(result["request_id"])
    assert request["pending"] is None
    assert request["effective"]["end_date"] == END_10
    balance = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (balance["used"], balance["reserved"]) == (10, 0)
    # Dates of the failed change are free again.
    other = desk.submit_request("E1", "annual", "2025-06-02", "2025-06-06",
                                submission_id="sub-other")
    assert other["request_id"]


def test_change_may_overlap_own_dates_only(desk):
    first = _approved_10(desk, submission_id="sub-1")
    # Another request right after: 2025-01-20 (Mon) .. 2025-01-21 (Tue).
    second = desk.submit_request("E1", "annual", "2025-01-20", "2025-01-21",
                                 submission_id="sub-2")
    desk.approve("sub-2")
    request = desk.get_request(first["request_id"])
    base = request["effective"]["version_id"]
    # Overlapping the *other* request's dates is rejected...
    with pytest.raises(DateOverlapError):
        desk.submit_change(first["request_id"], base, "annual",
                           "2025-01-17", "2025-01-21",
                           submission_id="sub-bad")
    # ...but overlapping only its own dates is fine.
    desk.submit_change(first["request_id"], base, "annual",
                       "2025-01-13", "2025-01-17", submission_id="sub-ok")
    desk.reject("sub-ok")
    assert desk.get_request(second["request_id"])["effective"] is not None
