"""ISO 日期小工具（仅标准库）。"""
from __future__ import annotations

from datetime import date, timedelta


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def add_days(value: str, days: int) -> str:
    return (parse_date(value) + timedelta(days=days)).isoformat()


def days_between(start: str, end: str) -> int:
    """含首尾的天数跨度；end 在 start 之前时为负。"""
    return (parse_date(end) - parse_date(start)).days


def iter_dates(start: str, end: str):
    cur = parse_date(start)
    last = parse_date(end)
    while cur <= last:
        yield cur.isoformat()
        cur += timedelta(days=1)


def shift_if_later(day: str, floor: str | None) -> str:
    if floor and floor > day:
        return floor
    return day
