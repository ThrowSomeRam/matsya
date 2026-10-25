"""Bucket a world's transactions into AA fetch windows.

A row's known_at (from Track B) is the earliest moment the bank could hand it over, so it
is also its posting time. A window holds the rows of one account whose known_at falls in
[start_ts, end_ts]. Ordinary windows are whole IST days: start_ts is midnight IST of the
first day, end_ts the last instant of the last day, and the fetch runs at fetch_hour IST
on the next day.

The declared ReBIT range (startDate, endDate) is what A4's coverage reads. It is the IST
date of start_ts through the last day the window holds in full.

Ends step by cadence_days. A window starts overlap_days before the last successful fetch's
end, which is how A2 re-requests from coverage: after a failed fetch the next window
reaches back over everything the failure lost. The split_day_windows fault (faults.py)
replaces both rules for the windows it hits: the window ends mid-day, and the next one
starts a few hours before that cut instead of days before it. Windows are only scheduled
while their fetch time is at or before the world's cut.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import polars as pl

IST = timezone(timedelta(hours=5, minutes=30))
MIN_OVERLAP_DAYS = 3


@dataclass(frozen=True)
class WindowConfig:
    cadence_days: int = 7
    overlap_days: int = 3
    fetch_hour_ist: int = 6

    def __post_init__(self):
        if self.cadence_days < 1:
            raise ValueError("cadence_days must be at least 1")
        if self.overlap_days < MIN_OVERLAP_DAYS:
            raise ValueError(f"overlap_days must be at least {MIN_OVERLAP_DAYS}")


@dataclass(frozen=True)
class Window:
    account_id: str
    index: int
    start_ts: datetime  # UTC, inclusive
    end_ts: datetime  # UTC, inclusive
    fetched_at: datetime  # UTC
    split: bool = False  # ends mid-day (split_day_windows)

    @property
    def start(self) -> date:
        return ist_date(self.start_ts)

    @property
    def end(self) -> date:
        """Last day held in full: the declared ReBIT endDate."""
        d = ist_date(self.end_ts)
        return d - timedelta(days=1) if self.split else d

    @property
    def payload_id(self) -> str:
        return f"PL-{self.account_id}-{self.index:04d}-{ist_date(self.end_ts).isoformat()}"


def ist_date(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def ist_start(d: date) -> datetime:
    return datetime.combine(d, time(), tzinfo=IST).astimezone(timezone.utc)


def ist_end(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59, 999999), tzinfo=IST).astimezone(timezone.utc)


def fetch_time(end: date, cfg: WindowConfig) -> datetime:
    return datetime.combine(end + timedelta(days=1), time(cfg.fetch_hour_ist), tzinfo=IST).astimezone(timezone.utc)


def end_dates(origin: date, cut: datetime, cfg: WindowConfig) -> list[date]:
    ends, end = [], origin + timedelta(days=cfg.cadence_days - 1)
    while fetch_time(end, cfg) <= cut:
        ends.append(end)
        end += timedelta(days=cfg.cadence_days)
    return ends


def rows_in(window: Window, rows: pl.DataFrame) -> pl.DataFrame:
    """Rows of the window's account posted inside it, in posting order."""
    return rows.filter((pl.col("account_id") == window.account_id)
                       & pl.col("known_at").is_between(window.start_ts, window.end_ts))


def with_posting(rows: pl.DataFrame) -> pl.DataFrame:
    """Add posted_ist and sort in posting order: (known_at, value_date, day_seq, txn_uid)."""
    return rows.with_columns(
        pl.col("known_at").dt.convert_time_zone("Asia/Kolkata").dt.date().alias("posted_ist"),
    ).sort("account_id", "known_at", "value_date", "day_seq", "txn_uid")
