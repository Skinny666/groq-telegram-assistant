from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True)
class BusyInterval:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class AvailableSlot:
    starts_at: datetime
    ends_at: datetime


def _ceil_to_interval(value: datetime, interval_minutes: int) -> datetime:
    discarded = value.minute % interval_minutes
    if discarded == 0 and value.second == 0 and value.microsecond == 0:
        return value
    delta = interval_minutes - discarded
    return (value + timedelta(minutes=delta)).replace(second=0, microsecond=0)


def _period_rank(value: datetime, preferred_period: str) -> int:
    hour = value.hour + value.minute / 60
    periods = {
        "morning": (8.0, 12.0),
        "afternoon": (12.0, 17.0),
        "evening": (17.0, 21.0),
    }
    if preferred_period in periods:
        start, end = periods[preferred_period]
        return 0 if start <= hour < end else 2

    # Sem preferência explícita, prioriza meio da manhã e meio da tarde.
    if 10.0 <= hour < 12.0 or 14.0 <= hour < 16.0:
        return 0
    return 1


def _overlaps(start: datetime, end: datetime, busy: BusyInterval) -> bool:
    return start < busy.ends_at and end > busy.starts_at


def find_available_slots(
    *,
    range_start: datetime,
    range_end: datetime,
    duration_minutes: int,
    preferred_period: str,
    result_limit: int,
    busy_intervals: Iterable[BusyInterval],
    local_timezone: ZoneInfo,
    workday_start: time,
    workday_end: time,
    interval_minutes: int,
    buffer_minutes: int,
    minimum_lead_minutes: int,
    now: datetime,
) -> list[AvailableSlot]:
    if range_start.tzinfo is None or range_end.tzinfo is None or now.tzinfo is None:
        raise ValueError("Datas precisam conter timezone.")
    if range_end <= range_start:
        return []

    local_now = now.astimezone(local_timezone)
    start_boundary = max(
        range_start.astimezone(local_timezone),
        local_now + timedelta(minutes=minimum_lead_minutes),
    )
    end_boundary = range_end.astimezone(local_timezone)

    expanded_busy: list[BusyInterval] = []
    buffer_delta = timedelta(minutes=buffer_minutes)
    for interval in busy_intervals:
        expanded_busy.append(
            BusyInterval(
                starts_at=interval.starts_at.astimezone(local_timezone) - buffer_delta,
                ends_at=interval.ends_at.astimezone(local_timezone) + buffer_delta,
            )
        )

    candidates: list[AvailableSlot] = []
    current_date: date = start_boundary.date()
    last_date: date = end_boundary.date()
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=interval_minutes)

    while current_date <= last_date:
        if current_date.weekday() < 5:
            day_start = datetime.combine(
                current_date, workday_start, tzinfo=local_timezone
            )
            day_end = datetime.combine(current_date, workday_end, tzinfo=local_timezone)
            scan_start = _ceil_to_interval(
                max(day_start, start_boundary), interval_minutes
            )
            scan_end = min(day_end, end_boundary)

            candidate_start = scan_start
            while candidate_start + duration <= scan_end:
                candidate_end = candidate_start + duration
                if not any(
                    _overlaps(candidate_start, candidate_end, busy)
                    for busy in expanded_busy
                ):
                    candidates.append(
                        AvailableSlot(
                            starts_at=candidate_start,
                            ends_at=candidate_end,
                        )
                    )
                candidate_start += step

        current_date += timedelta(days=1)

    candidates.sort(
        key=lambda slot: (
            _period_rank(slot.starts_at, preferred_period),
            slot.starts_at,
        )
    )
    return candidates[: max(1, min(result_limit, 20))]
