from urllib import parse

from golem import ledger, t3_code


def thread_link(task: ledger.Task, conversation_id: str) -> str:
    environment_id = (t3_code.t3_home() / "userdata" / "environment-id").read_text(encoding="utf-8")
    encoded_thread_id = parse.quote(t3_code.task_thread_id(task, conversation_id), safe="")
    return f"t3code://threads/{environment_id.strip()}/{encoded_thread_id}"


def ensure_thread(
    task: ledger.Task, conversation_id: str, thread_id: str, instance: t3_code.ClaudeInstance | None
) -> None:
    t3_code.ensure_app_running()
    is_present = t3_code.is_thread_present(thread_id)
    if is_present and instance is None:
        return
    if not is_present and task["backend"] == ledger.BACKEND_T3:
        raise t3_code.T3CodeError("thread does not exist in T3 Code")
    origin = t3_code.server_origin()
    token = t3_code.issue_token()
    if instance is not None:
        t3_code.upsert_claude_instance(origin, token, instance)
    if is_present:
        return
    driver = t3_code.INSTANCE_IDS[task["backend"]]
    command = t3_code.thread_create_command(
        origin,
        token,
        task,
        thread_id,
        t3_code.thread_instance_id(driver, instance),
        t3_code.default_model(driver),
    )
    command["importedNativeThread"] = {
        "ref": {"driver": driver, "nativeId": conversation_id, "strength": "strong"}
    }
    t3_code.dispatch_command(origin, token, command)
    if not t3_code.is_thread_present(thread_id):
        raise t3_code.T3CodeError("thread is missing after thread.create")


def prepare_thread(
    task: ledger.Task, conversation_id: str, instance: t3_code.ClaudeInstance | None
) -> None:
    cli_path = t3_code.t3_cli_path()
    if not cli_path.exists():
        raise SystemExit(f"golem: T3 Code is not installed: {cli_path} not found")
    thread_id = t3_code.task_thread_id(task, conversation_id)
    try:
        ensure_thread(task, conversation_id, thread_id, instance)
    except t3_code.T3_CODE_ERRORS as error:
        raise SystemExit(
            f"golem: T3 Code {t3_code.version()} could not open thread {thread_id}: {error}"
        ) from error
