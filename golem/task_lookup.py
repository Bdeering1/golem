import sys

from golem import display, ledger


def picker_lines(tasks: ledger.Tasks, task_ids: list[str]) -> list[str]:
    rows = [
        (
            f"{number})",
            task_id,
            tasks[task_id].get("label", ""),
            tasks[task_id]["status"],
            display.display_directory(tasks[task_id]["cwd"]),
            display.first_line(tasks[task_id]["prompt"]),
        )
        for number, task_id in enumerate(task_ids, start=1)
    ]
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]) - 1)]
    return [
        "  ".join(
            [*(value.ljust(width) for value, width in zip(row, widths, strict=False)), row[-1]]
        )
        for row in rows
    ]


def picked_task_id(task_ids: list[str]) -> str:
    while True:
        try:
            answer = input(f"Pick a task [1-{len(task_ids)}]: ").strip()
        except EOFError, KeyboardInterrupt:
            print()
            raise SystemExit("golem: cancelled") from None
        if not answer:
            raise SystemExit("golem: cancelled")
        if answer.isdigit() and 1 <= int(answer) <= len(task_ids):
            return task_ids[int(answer) - 1]


def resolved_task_id(argument: str) -> str:
    tasks = ledger.load_tasks()
    if argument in tasks:
        return argument
    matching_ids = [
        task_id for task_id, task in reversed(tasks.items()) if task.get("label") == argument
    ]
    if not matching_ids:
        raise SystemExit(f"golem: task not found: {argument}")
    if len(matching_ids) == 1:
        return matching_ids[0]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit(f"golem: label {argument} matches tasks: {', '.join(matching_ids)}")
    for line in picker_lines(tasks, matching_ids):
        print(line)
    return picked_task_id(matching_ids)
