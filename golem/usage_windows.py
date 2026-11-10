import json
import re
import sys
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from pathlib import Path

from golem import config, ledger, notifications
from golem.backends import registry

TIME_PATTERN = re.compile(r"(1[0-2]|[1-9])(?::([0-5][0-9]))?(am|pm)( tomorrow)?")
USAGE_WINDOW_CUTOFF = timedelta(minutes=30)


class UsageWindowRecord(TypedDict):
    date: str
    is_settled: bool
    error: NotRequired[str]


def parsed_time(value: str) -> tuple[time, bool] | None:
    match = TIME_PATTERN.fullmatch(value.lower())
    if match is None:
        return None
    hour, minute, period, tomorrow = match.groups()
    time_of_day = time(int(hour) % 12 + (12 if period == "pm" else 0), int(minute or 0))
    return time_of_day, tomorrow is not None


def usage_window_times() -> dict[str, list[time]]:
    table = config.load_config().get("usage_windows")
    if table is None:
        return {}
    message_prefix = f"{config.invalid_config_message_prefix()}: usage_windows"
    if not isinstance(table, dict):
        raise SystemExit(f"{message_prefix} must be a table")
    window_times: dict[str, list[time]] = {}
    for backend_id, values in cast("dict[str, object]", table).items():
        entry_message_prefix = f"{message_prefix}.{backend_id}"
        if backend_id not in registry.USAGE_BACKENDS:
            raise SystemExit(f"{entry_message_prefix} is not claude or codex")
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in cast("list[object]", values)
        ):
            raise SystemExit(f"{entry_message_prefix} must be a list of daily times like 6am")
        window_times[backend_id] = []
        for value in cast("list[str]", values):
            parsed = parsed_time(value)
            if parsed is None or parsed[1]:
                raise SystemExit(f"{entry_message_prefix} has invalid time {value}")
            window_times[backend_id].append(parsed[0])
    return window_times


def usage_window_state_path() -> Path:
    return ledger.state_directory() / "usage_windows.json"


def latest_occurrence(time_of_day: time, now: datetime) -> datetime:
    local_today = now.astimezone().date()
    occurrence = datetime.combine(local_today, time_of_day).astimezone()
    if occurrence <= now:
        return occurrence
    return datetime.combine(local_today - timedelta(days=1), time_of_day).astimezone()


def open_due_usage_windows(
    window_times: dict[str, list[time]], environment: dict[str, str], now: datetime
) -> None:
    state_path = usage_window_state_path()
    records = cast("dict[str, UsageWindowRecord]", ledger.load_json(state_path))
    original_records_text = json.dumps(records)
    for backend_id, times in window_times.items():
        backend = registry.USAGE_BACKENDS[backend_id]
        for time_of_day in times:
            occurrence = latest_occurrence(time_of_day, now)
            occurrence_date = occurrence.date().isoformat()
            key = f"{backend_id} {time_of_day:%H:%M}"
            record = records.get(key)
            if record is None or record["date"] != occurrence_date:
                record = None
            elif record["is_settled"]:
                continue
            label = f"{backend.NAME} usage window for {occurrence:%Y-%m-%d %H:%M}"
            if now - occurrence >= USAGE_WINDOW_CUTOFF:
                records[key] = {"date": occurrence_date, "is_settled": True}
                error = None if record is None else record.get("error")
                if error is None:
                    late_minutes = int(USAGE_WINDOW_CUTOFF.total_seconds() // 60)
                    print(
                        f"golem: skipped {label}, {late_minutes} or more minutes late",
                        file=sys.stderr,
                    )
                    continue
                print(f"golem: gave up opening {label}: {error}", file=sys.stderr)
                notifications.notify(
                    f"golem: could not open {backend.NAME} usage window",
                    f"{occurrence:%H:%M}: {error}"[: notifications.NOTIFICATION_MESSAGE_MAX_LENGTH],
                )
                continue
            error = backend.open_usage_window(environment)
            if error is None:
                print(f"golem: opened {label}", file=sys.stderr)
                records[key] = {"date": occurrence_date, "is_settled": True}
                continue
            print(f"golem: could not open {label}: {error}", file=sys.stderr)
            records[key] = {"date": occurrence_date, "is_settled": False, "error": error}
    if json.dumps(records) != original_records_text:
        ledger.save_json(state_path, records)
