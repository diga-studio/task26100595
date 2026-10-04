"""Date helpers: inclusive ranges, weekday counting, per-year split."""

from __future__ import annotations

import datetime as _dt


def parse_date(value) -> _dt.date:
    """Normalize a date-like value to ``datetime.date``."""
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        return _dt.date.fromisoformat(value.strip())
    raise TypeError(f"unsupported date value: {value!r}")


def iter_days(start: _dt.date, end: _dt.date):
    """Yield every calendar day in the inclusive range [start, end]."""
    if end < start:
        raise ValueError(f"end {end} before start {start}")
    day = start
    one = _dt.timedelta(days=1)
    while day <= end:
        yield day
        day += one


def chargeable_days_by_year(start: _dt.date, end: _dt.date) -> dict[int, int]:
    """Count Mon-Fri days in [start, end], grouped by calendar year."""
    result: dict[int, int] = {}
    for day in iter_days(start, end):
        if day.weekday() < 5:  # Mon=0 .. Fri=4
            result[day.year] = result.get(day.year, 0) + 1
    return result


def occupied_dates(start: _dt.date, end: _dt.date) -> list[str]:
    """All ISO calendar dates in [start, end] (weekends included)."""
    return [day.isoformat() for day in iter_days(start, end)]
