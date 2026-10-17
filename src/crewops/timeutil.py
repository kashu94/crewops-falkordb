"""Time helpers. The graph stores times as Unix seconds and dates as YYYY-MM-DD strings."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from crewops.config import parse_utc


def ts(value: str | datetime) -> int:
    """ISO text or a datetime to Unix seconds."""
    dt = parse_utc(value) if isinstance(value, str) else value
    return int(dt.timestamp())


def utc(seconds: int | float) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def iso(seconds: int | float) -> str:
    """``2026-10-13T02:30:00Z``."""
    return utc(seconds).strftime("%Y-%m-%dT%H:%M:%SZ")


def hhmm(seconds: int | float) -> str:
    return utc(seconds).strftime("%H:%M")


def day_of(seconds: int | float) -> str:
    return utc(seconds).date().isoformat()


def add_days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def hours(start: int | float, end: int | float) -> float:
    """Elapsed hours, rounded to two decimals."""
    return round((end - start) / 3600.0, 2)


def format_hours(value: float) -> str:
    """``1.33`` -> ``'1h20m'``."""
    minutes = round(abs(value) * 60)
    return f"{minutes // 60}h{minutes % 60:02d}m"
