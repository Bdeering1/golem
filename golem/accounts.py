import json
import os
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Sequence

from golem import ledger, t3_code

CONFIG_DIRECTORY_VARIABLE = "CLAUDE_CONFIG_DIR"
TOKEN_VARIABLES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
PRIVATE_ENTRY_NAMES = frozenset(
    {".claude.json", ".claude.json.lock", "backups", ".credentials.json"}
)
NORMAL_LOGIN_NUMBER = 0
UNKNOWN = "unknown"
CLAUDE_AUTH_COMMAND = ("claude", "auth")
ACCOUNT_LABEL = "{email} ({org_name})"
T3_INSTANCE_ID_TEMPLATE = "golem-claude-{number}"


class Account(TypedDict):
    email: str
    org_id: str
    org_name: str


class AccountRecords(TypedDict):
    next_number: int
    accounts: dict[str, Account]


def accounts_directory() -> Path:
    return ledger.state_directory() / "accounts"


def account_directory(number: int) -> Path:
    return accounts_directory() / str(number)


def account_records_path() -> Path:
    return ledger.state_directory() / "accounts.json"


def load_account_records() -> AccountRecords:
    stored = ledger.load_json(account_records_path())
    return {
        "next_number": cast("int", stored.get("next_number", 1)),
        "accounts": cast("dict[str, Account]", stored.get("accounts", {})),
    }


def is_account_config_directory(path_text: str) -> bool:
    return Path(path_text).resolve().is_relative_to(accounts_directory().resolve())


def link_missing_entries(source_directory: Path, shadow_directory: Path) -> None:
    if not source_directory.is_dir():
        return
    for source_path in source_directory.iterdir():
        shadow_path = shadow_directory / source_path.name
        if source_path.name in PRIVATE_ENTRY_NAMES or shadow_path.exists(follow_symlinks=False):
            continue
        with suppress(FileExistsError):
            shadow_path.symlink_to(source_path)


def account_environment(environment: dict[str, str], account_number: int | None) -> dict[str, str]:
    environment = dict(environment)
    config_directory = environment.get(CONFIG_DIRECTORY_VARIABLE)
    if config_directory is not None and is_account_config_directory(config_directory):
        del environment[CONFIG_DIRECTORY_VARIABLE]
    if account_number is None:
        return environment
    shadow_directory = account_directory(account_number)
    if not shadow_directory.is_dir():
        raise SystemExit(f"golem: account {account_number} was removed")
    source_directory = environment.get(CONFIG_DIRECTORY_VARIABLE, str(Path.home() / ".claude"))
    link_missing_entries(Path(source_directory), shadow_directory)
    for name in TOKEN_VARIABLES:
        environment.pop(name, None)
    environment[CONFIG_DIRECTORY_VARIABLE] = str(shadow_directory)
    return environment


def t3_instance_id(number: int) -> str:
    return T3_INSTANCE_ID_TEMPLATE.format(number=number)


def t3_claude_instance(account_number: int | None) -> t3_code.ClaudeInstance | None:
    if account_number is None:
        return None
    environment = account_environment(os.environ.copy(), account_number)
    account = load_account_records()["accounts"][str(account_number)]
    return t3_code.ClaudeInstance(
        t3_instance_id(account_number),
        ACCOUNT_LABEL.format_map(account),
        environment[CONFIG_DIRECTORY_VARIABLE],
    )


def logged_in_account(environment: dict[str, str]) -> Account | None:
    try:
        result = subprocess.run(
            [*CLAUDE_AUTH_COMMAND, "status", "--json"],
            env=environment,
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        status = cast("object", json.loads(result.stdout))
    except OSError, json.JSONDecodeError:
        return None
    if result.returncode != 0 or not isinstance(status, dict):
        return None
    fields = cast("dict[str, object]", status)
    email, org_id, org_name = fields.get("email"), fields.get("orgId"), fields.get("orgName")
    if (
        fields.get("loggedIn") is not True
        or not isinstance(email, str)
        or not isinstance(org_id, str)
        or not isinstance(org_name, str)
    ):
        return None
    return {"email": email, "org_id": org_id, "org_name": org_name}


def normal_login() -> Account | None:
    return logged_in_account(account_environment(os.environ.copy(), None))


def extra_accounts(records: AccountRecords) -> list[tuple[int, Account]]:
    return [(int(number), account) for number, account in records["accounts"].items()]


def known_accounts(records: AccountRecords) -> list[tuple[int, Account]]:
    login = normal_login()
    accounts = extra_accounts(records)
    return accounts if login is None else [(NORMAL_LOGIN_NUMBER, login), *accounts]


def account_lines(accounts: Sequence[tuple[int, Account | None]]) -> list[str]:
    columns = [
        (account["email"], account["org_name"]) if account else (UNKNOWN, UNKNOWN)
        for _, account in accounts
    ]
    email_width = max(len(email) for email, _ in columns)
    org_width = max(len(org_name) for _, org_name in columns)
    return [
        f"{email:<{email_width}}  {org_name:<{org_width}}  {number}"
        for (email, org_name), (number, _) in zip(columns, accounts, strict=True)
    ]


def resolved_account_number(value: str) -> int:
    records = load_account_records()
    unknown_error = SystemExit(f"golem: unknown account: {value}")
    if value.isdigit():
        number = int(value)
        if number != NORMAL_LOGIN_NUMBER and str(number) not in records["accounts"]:
            raise unknown_error
        return number
    candidates = known_accounts(records)
    lowered = value.lower()
    matches = [item for item in candidates if item[1]["email"].lower() == lowered] or [
        item for item in candidates if item[1]["email"].lower().startswith(lowered)
    ]
    if not matches:
        raise unknown_error
    if len(matches) > 1:
        lines = "\n".join(account_lines(matches))
        raise SystemExit(f"golem: {value} matches more than one account; use its number:\n{lines}")
    return matches[0][0]


def task_account_number(value: str | None) -> int | None:
    if value is None:
        return None
    number = resolved_account_number(value)
    return None if number == NORMAL_LOGIN_NUMBER else number


def run_auth_logout(environment: dict[str, str]) -> int:
    return subprocess.run([*CLAUDE_AUTH_COMMAND, "logout"], env=environment, check=False).returncode


def is_same_account(first: Account, second: Account) -> bool:
    return first["email"] == second["email"] and first["org_id"] == second["org_id"]


def sign_in_new_account(records: AccountRecords, environment: dict[str, str]) -> Account:
    if subprocess.run([*CLAUDE_AUTH_COMMAND, "login"], env=environment, check=False).returncode:
        raise SystemExit("golem: Claude login failed")
    account = logged_in_account(environment)
    if account is None:
        run_auth_logout(environment)
        raise SystemExit("golem: Claude login failed")
    if any(is_same_account(account, other) for _, other in known_accounts(records)):
        run_auth_logout(environment)
        raise SystemExit(f"golem: {ACCOUNT_LABEL.format_map(account)} is already signed in")
    return account


def add_account() -> int:
    records = load_account_records()
    number = records["next_number"]
    directory = account_directory(number)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        account = sign_in_new_account(records, account_environment(os.environ.copy(), number))
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    records["accounts"][str(number)] = account
    records["next_number"] = number + 1
    ledger.save_json(account_records_path(), records)
    print(f"golem: added {ACCOUNT_LABEL.format_map(account)}")
    return 0


def list_accounts() -> int:
    records = load_account_records()
    accounts: list[tuple[int, Account | None]] = [
        (NORMAL_LOGIN_NUMBER, normal_login()),
        *extra_accounts(records),
    ]
    print("\n".join(account_lines(accounts)))
    return 0


def remove_account(value: str) -> int:
    number = resolved_account_number(value)
    if number == NORMAL_LOGIN_NUMBER:
        raise SystemExit("golem: cannot remove the normal Claude login")
    unfinished_task_ids = [
        task_id
        for task_id, task in ledger.load_tasks().items()
        if task.get("account") == number and task.get("status") in ledger.UNREVIEWED_STATUSES
    ]
    if unfinished_task_ids:
        raise SystemExit(
            f"golem: account is used by unfinished tasks: {', '.join(unfinished_task_ids)}"
        )
    try:
        t3_code.remove_claude_instance(t3_instance_id(number))
    except t3_code.T3_CODE_ERRORS as error:
        raise SystemExit(
            f"golem: T3 Code could not remove the account: {error}; the account was not removed"
        ) from error
    if run_auth_logout(account_environment(os.environ.copy(), number)) != 0:
        raise SystemExit("golem: Claude logout failed; the account was not removed")
    shutil.rmtree(account_directory(number))
    records = load_account_records()
    account = records["accounts"].pop(str(number))
    ledger.save_json(account_records_path(), records)
    print(f"golem: removed {ACCOUNT_LABEL.format_map(account)}")
    return 0
