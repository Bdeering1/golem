from golem import board, display, ledger, picker


def picker_rows(tasks: ledger.Tasks, task_ids: list[str]) -> list[tuple[str, ...]]:
    return [
        (
            task_id,
            tasks[task_id].get("label", ""),
            tasks[task_id]["status"],
            display.display_directory(tasks[task_id]["cwd"]),
            display.first_line(tasks[task_id]["prompt"]),
        )
        for task_id in task_ids
    ]


def resolved_task_id(argument: str, *, is_unknown_kept: bool = False) -> str:
    tasks = ledger.load_tasks()
    if argument in tasks:
        return argument
    matching_ids = [
        task_id for task_id, task in reversed(tasks.items()) if task.get("label") == argument
    ]
    if not matching_ids and is_unknown_kept:
        return argument
    if not matching_ids:
        raise SystemExit(f"golem: task not found: {argument}")
    if len(matching_ids) == 1:
        return matching_ids[0]
    not_a_terminal_message = f"golem: label {argument} matches tasks: {', '.join(matching_ids)}"
    if not board.is_terminal():
        raise SystemExit(not_a_terminal_message)
    try:
        picked_indices = picker.picked_indices(
            f"Pick a task labelled {argument}",
            picker_rows(tasks, matching_ids),
            "pick",
            is_multi_select=False,
        )
    except board.TerminalStartError:
        raise SystemExit(not_a_terminal_message) from None
    except KeyboardInterrupt:
        raise SystemExit(board.INTERRUPTED_EXIT_STATUS) from None
    if picked_indices is None:
        raise SystemExit(picker.CANCELLED_MESSAGE)
    return matching_ids[picked_indices.pop()]
