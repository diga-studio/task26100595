"""Concurrency across instances on one file, and reopen persistence."""

import threading

from peopledesk import PeopleDesk
from peopledesk.errors import PeopleDeskError


def _run_parallel(db_path, worker, count=16):
    """Run worker(index, desk) in `count` threads, each with its own
    PeopleDesk instance opened on the same file."""
    results = [None] * count
    errors = [None] * count
    barrier = threading.Barrier(count)

    def run(index):
        desk = PeopleDesk(db_path)
        try:
            barrier.wait(timeout=10)
            results[index] = worker(index, desk)
        except Exception as exc:  # noqa: BLE001 - collected for asserts
            errors[index] = exc
        finally:
            desk.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "threads stuck"
    return results, errors


def test_16_threads_compete_for_last_day(tmp_path):
    db_path = str(tmp_path / "leave.db")
    desk = PeopleDesk(db_path)
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-23",
                        submission_id="sub-14")
    desk.approve("sub-14")  # 14 of 15 used, 1 day left
    desk.close()

    def worker(index, desk):
        # Distinct dates per worker: only the quota decides.
        # 2025-02-03..07, 10..14, 17..21, 24..27: 16 distinct weekdays.
        weekdays = [
            f"2025-02-{day:02d}"
            for day in (3, 4, 5, 6, 7, 10, 11, 12, 13, 14,
                        17, 18, 19, 20, 21, 24)
        ]
        date = weekdays[index]
        desk.submit_request("E1", "annual", date, date,
                            submission_id=f"sub-w{index}")
        return index

    results, errors = _run_parallel(db_path, worker)
    winners = [r for r in results if r is not None]
    losers = [e for e in errors if isinstance(e, PeopleDeskError)]
    assert len(winners) == 1
    assert len(losers) == 15
    # No thread saw a raw database error instead of a business verdict.
    assert all(isinstance(e, PeopleDeskError) for e in errors if e)

    desk = PeopleDesk(db_path)
    balance = desk.get_balance("E1", 2025)["types"]["annual"]
    assert (balance["used"], balance["reserved"], balance["available"]) == (
        14, 1, 0)
    desk.close()


def test_16_threads_compete_for_same_dates(tmp_path):
    db_path = str(tmp_path / "leave.db")

    def worker(index, desk):
        desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                            submission_id=f"sub-d{index}")
        return index

    results, errors = _run_parallel(db_path, worker)
    assert len([r for r in results if r is not None]) == 1
    assert len([e for e in errors if isinstance(e, PeopleDeskError)]) == 15


def test_concurrent_approve_reject_single_verdict(tmp_path):
    db_path = str(tmp_path / "leave.db")
    desk = PeopleDesk(db_path)
    desk.submit_request("E1", "annual", "2025-01-06", "2025-01-10",
                        submission_id="sub-race")
    desk.close()

    def worker(index, desk):
        if index % 2 == 0:
            return desk.approve("sub-race")["verdict"]
        return desk.reject("sub-race")["verdict"]

    results, errors = _run_parallel(db_path, worker)
    verdicts = [r for r in results if r is not None]
    conflicts = [e for e in errors if isinstance(e, PeopleDeskError)]
    # Every caller got an answer; all verdicts agree on one final outcome.
    assert len(verdicts) + len(conflicts) == 16
    assert len(set(verdicts)) == 1
    assert len(verdicts) >= 1

    desk = PeopleDesk(db_path)
    final = "approved" if verdicts[0] == "approved" else "rejected"
    request = desk.get_request(
        desk._conn.execute("SELECT request_id FROM requests").fetchone()[0])
    if final == "approved":
        assert request["effective"] is not None
        assert desk.get_balance("E1", 2025)["types"]["annual"]["used"] == 5
    else:
        assert request["effective"] is None
        assert request["pending"] is None
        assert desk.get_balance("E1", 2025)["types"]["annual"]["used"] == 0
    desk.close()


def test_state_survives_reopen(tmp_path):
    db_path = str(tmp_path / "leave.db")
    desk = PeopleDesk(db_path)
    result = desk.submit_request("E1", "annual", "2024-12-30", "2025-01-10",
                                 submission_id="sub-p1")
    desk.approve("sub-p1")
    request = desk.get_request(result["request_id"])
    desk.submit_change(result["request_id"],
                       request["effective"]["version_id"], "sick",
                       "2025-03-03", "2025-03-07", submission_id="sub-p2")
    before_request = desk.get_request(result["request_id"])
    before_bal24 = desk.get_balance("E1", 2024)
    before_bal25 = desk.get_balance("E1", 2025)
    desk.close()

    desk = PeopleDesk(db_path)
    assert desk.get_request(result["request_id"]) == before_request
    assert desk.get_balance("E1", 2024) == before_bal24
    assert desk.get_balance("E1", 2025) == before_bal25
    # Approval verdicts survive too.
    replay = desk.approve("sub-p1")
    assert replay["replayed"] is True
    # And the pending change can still be decided after reopen.
    desk.reject("sub-p2")
    assert desk.get_request(result["request_id"])["pending"] is None
    desk.close()
