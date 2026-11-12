import json
import re
import sys
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, NamedTuple, NotRequired, TypedDict, cast

if TYPE_CHECKING:
    from pathlib import Path

    from golem.backends import common

from golem import accounts, config, ledger, notifications
from golem.backends import registry

TIME_PATTERN = re.compile(r"(1[0-2]|[1-9])(?::([0-5][0-9]))?(am|pm)( tomorrow)?")
USAGE_WINDOW_CUTOFF = timedelta(minutes=30)


class UsageWindowRecord(TypedDict):
    date: str
    is_settled: bool
    error: NotRequired[str]


class UsageWindowEntry(NamedTuple):
    backend_id: str
    account: tuple[int, accounts.Account] | None
    times: list[time]


def parsed_time(value: str) -> tuple[time, bool] | None:
    match = TIME_PATTERN.fullmatch(value.lower())
    if match is None:
        return None
    hour, minute, period, tomorrow = match.groups()
    time_of_day = time(int(hour) % 12 + (12 if period == "pm" else 0), int(minute or 0))
    return time_of_day, tomorrow is not None


def configured_account(
    key: str, entry_message_prefix: str, environment: dict[str, str]
) -> tuple[int, accounts.Account] | None:
    matches = accounts.matching_accounts(
        key, accounts.load_account_records(), environment, is_prefix_allowed=False
    )
    if not matches:
        raise SystemExit(f"{entry_message_prefix} matches no Claude account")
    if len(matches) > 1:
        raise SystemExit(accounts.ambiguous_account_message(entry_message_prefix, matches))
    number, account = matches[0]
    if number == accounts.NORMAL_LOGIN_NUMBER or account is None:
        return None
    return number, account


def usage_window_entries(environment: dict[str, str]) -> list[UsageWindowEntry]:
    table = config.load_config().get("usage_windows")
    if table is None:
        return []
    message_prefix = f"{config.invalid_config_message_prefix()}: usage_windows"
    if not isinstance(table, dict):
        raise SystemExit(f"{message_prefix} must be a table")
    window_entries: list[UsageWindowEntry] = []
    keys_by_account: dict[tuple[str, int], str] = {}
    for key, values in cast("dict[str, object]", table).items():
        entry_message_prefix = f"{message_prefix}.{key}"
        if key in registry.USAGE_BACKENDS:
            backend_id, account = key, None
        else:
            backend_id = ledger.BACKEND_CLAUDE
            account = configured_account(key, entry_message_prefix, environment)
        account_number = accounts.NORMAL_LOGIN_NUMBER if account is None else account[0]
        other_key = keys_by_account.get((backend_id, account_number))
        if other_key is not None:
            raise SystemExit(
                f"{entry_message_prefix} lists the same account as usage_windows.{other_key}"
            )
        keys_by_account[backend_id, account_number] = key
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in cast("list[object]", values)
        ):
            raise SystemExit(f"{entry_message_prefix} must be a list of daily times like 6am")
        times: list[time] = []
        for value in cast("list[str]", values):
            parsed = parsed_time(value)
            if parsed is None:
                raise SystemExit(f"{entry_message_prefix} has invalid time {value}")
            time_of_day, is_tomorrow = parsed
            if is_tomorrow:
                raise SystemExit(f"{entry_message_prefix} has invalid time {value}")
            times.append(time_of_day)
        window_entries.append(UsageWindowEntry(backend_id, account, times))
    return window_entries


def usage_window_state_path() -> Path:
    return ledger.state_directory() / "usage_windows.json"


def latest_occurrence(time_of_day: time, now: datetime) -> datetime:
    local_today = now.astimezone().date()
    occurrence = datetime.combine(local_today, time_of_day).astimezone()
    if occurrence <= now:
        return occurrence
    return datetime.combine(local_today - timedelta(days=1), time_of_day).astimezone()


def usage_window_error(
    backend: common.UsageBackend, account_number: int, environment: dict[str, str]
) -> str | None:
    if account_number == accounts.NORMAL_LOGIN_NUMBER:
        return backend.open_usage_window(environment)
    try:
        account_environment = accounts.account_environment(environment, account_number)
    except SystemExit as error:
        return str(error).removeprefix("golem: ")
    return backend.open_usage_window(account_environment)


def open_due_usage_windows(
    window_entries: list[UsageWindowEntry], environment: dict[str, str], now: datetime
) -> None:
    state_path = usage_window_state_path()
    records = cast("dict[str, UsageWindowRecord]", ledger.load_json(state_path))
    original_records_text = json.dumps(records)
    for backend_id, account, times in window_entries:
        backend = registry.USAGE_BACKENDS[backend_id]
        account_number = accounts.NORMAL_LOGIN_NUMBER
        window_name = f"{backend.NAME} usage window"
        key_prefix = backend_id
        occurrence_preposition = "for"
        if account is not None:
            account_number, account_record = account
            window_name += f" for {accounts.ACCOUNT_LABEL.format_map(account_record)}"
            key_prefix += f" account {account_number}"
            occurrence_preposition = "at"
        for time_of_day in times:
            occurrence = latest_occurrence(time_of_day, now)
            occurrence_date = occurrence.date().isoformat()
            key = f"{key_prefix} {time_of_day:%H:%M}"
            record = records.get(key)
            if record is None or record["date"] != occurrence_date:
                record = None
            elif record["is_settled"]:
                continue
            label = f"{window_name} {occurrence_preposition} {occurrence:%Y-%m-%d %H:%M}"
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
                    f"golem: could not open {window_name}",
                    f"{occurrence:%H:%M}: {error}"[: notifications.NOTIFICATION_MESSAGE_MAX_LENGTH],
                )
                continue
            error = usage_window_error(backend, account_number, environment)
            if error is None:
                print(f"golem: opened {label}", file=sys.stderr)
                records[key] = {"date": occurrence_date, "is_settled": True}
                continue
            print(f"golem: could not open {label}: {error}", file=sys.stderr)
            records[key] = {"date": occurrence_date, "is_settled": False, "error": error}
    if json.dumps(records) != original_records_text:
        ledger.save_json(state_path, records)
