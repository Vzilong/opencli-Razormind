"""Timezone-aware occurrence matching for the Automation schedule contract."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SCHEDULE_RE = re.compile(r"^(daily|weekdays|weekly)@(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class AutomationScheduleSpec:
    kind: str
    hour: int | None = None
    minute: int = 0


def parse_automation_schedule(value: str) -> AutomationScheduleSpec:
    if value == "hourly":
        return AutomationScheduleSpec(kind="hourly")
    match = _SCHEDULE_RE.fullmatch(value)
    if match is None:
        raise ValueError(
            "schedule must be hourly, daily@HH:MM, weekdays@HH:MM, or weekly@HH:MM"
        )
    kind, raw_hour, raw_minute = match.groups()
    hour = int(raw_hour)
    minute = int(raw_minute)
    if hour > 23 or minute > 59:
        raise ValueError("schedule time must be a valid 24-hour HH:MM value")
    return AutomationScheduleSpec(kind=kind, hour=hour, minute=minute)


def parse_automation_timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown automation timezone: {value}") from exc


def automation_fire_times(
    schedule: str,
    timezone_name: str,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Match UTC minutes after local conversion for deterministic DST behavior."""
    if window_start.tzinfo is None or window_end.tzinfo is None:
        raise ValueError("automation scheduler windows must be timezone-aware")
    if window_end <= window_start:
        return []
    spec = parse_automation_schedule(schedule)
    timezone = parse_automation_timezone(timezone_name)
    cursor = window_start.astimezone(UTC).replace(second=0, microsecond=0)
    if cursor <= window_start.astimezone(UTC):
        cursor += timedelta(minutes=1)
    end_utc = window_end.astimezone(UTC)
    fires: list[datetime] = []
    while cursor <= end_utc:
        local = cursor.astimezone(timezone)
        matches_time = (
            local.minute == 0
            if spec.kind == "hourly"
            else local.hour == spec.hour and local.minute == spec.minute
        )
        matches_day = (
            spec.kind not in {"weekdays", "weekly"}
            or (spec.kind == "weekdays" and local.weekday() < 5)
            or (spec.kind == "weekly" and local.weekday() == 0)
        )
        if matches_time and matches_day:
            fires.append(cursor)
        cursor += timedelta(minutes=1)
    return fires
