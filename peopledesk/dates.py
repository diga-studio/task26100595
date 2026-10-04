"""日期工具: 工作日计费与自然日展开。"""
from __future__ import annotations

from datetime import date, timedelta


def daterange_days(start: date, end: date) -> list[date]:
    """展开区间内所有自然日(含首尾)。"""
    if end < start:
        raise ValueError("end_date 必须不早于 start_date")
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def chargeable_days_by_year(start: date, end: date) -> dict[int, int]:
    """按每个工作日所在年份分摊计费天数(周一至周五每天 1 天)。"""
    result: dict[int, int] = {}
    for day in daterange_days(start, end):
        if day.weekday() < 5:  # 0=周一 .. 4=周五
            result[day.year] = result.get(day.year, 0) + 1
    return result
