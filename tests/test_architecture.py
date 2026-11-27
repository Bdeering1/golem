import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent / "golem"
LAYER_BANS = {"golem.backends": "golem.clients"}
CAST_COUNTS = {
    "golem.accounts": 4,
    "golem.backends.claude": 11,
    "golem.backends.claude_hooks": 8,
    "golem.backends.codex": 20,
    "golem.backends.t3": 13,
    "golem.cli": 4,
    "golem.clients.codex_app": 1,
    "golem.delete": 2,
    "golem.gates": 2,
    "golem.ledger": 4,
    "golem.t3_code": 22,
    "golem.task_ids": 1,
    "golem.task_setup": 2,
    "golem.usage_windows": 5,
    "golem.worktrees.branch_pull_requests": 2,
    "golem.worktrees.task_directories": 2,
    "golem.worktrees.worktree_cleanup": 5,
}


def module_name(path: Path) -> str:
    return ".".join(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)


def is_type_checking_block(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and ast.unparse(node.test).endswith("TYPE_CHECKING")


def runtime_imports(tree: ast.AST) -> list[ast.Import | ast.ImportFrom]:
    found: list[ast.Import | ast.ImportFrom] = []
    pending = [tree]
    while pending:
        node = pending.pop()
        if is_type_checking_block(node):
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return found


def imported_modules(node: ast.Import | ast.ImportFrom, modules: set[str]) -> set[str]:
    if isinstance(node, ast.Import):
        return {alias.name for alias in node.names} & modules
    base = node.module or ""
    submodules = {f"{base}.{alias.name}" for alias in node.names} & modules
    return submodules or ({base} & modules)


def import_graph() -> dict[str, set[str]]:
    paths = {module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}
    modules = set(paths)
    return {
        name: set().union(
            *(
                imported_modules(node, modules)
                for node in runtime_imports(ast.parse(path.read_text(encoding="utf-8")))
            )
        )
        for name, path in paths.items()
    }


def find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    finished: set[str] = set()

    def visit(name: str, path: list[str]) -> list[str] | None:
        if name in path:
            return [*path[path.index(name) :], name]
        if name in finished:
            return None
        for imported in sorted(graph[name]):
            if cycle := visit(imported, [*path, name]):
                return cycle
        finished.add(name)
        return None

    return next(filter(None, (visit(name, []) for name in sorted(graph))), None)


def test_golem_modules_have_no_import_cycle():
    cycle = find_cycle(import_graph())

    assert cycle is None, (
        f"import cycle {' -> '.join(cycle or [])}: move the shared rule into the lower module "
        "(ledger for task-record predicates, chains for chain predicates) instead of importing up"
    )


def test_lower_layers_do_not_import_higher_layers():
    violations = [
        f"{name} imports {imported}"
        for name, imports in import_graph().items()
        for lower, higher in LAYER_BANS.items()
        if name.startswith(lower)
        for imported in imports
        if imported.startswith(higher)
    ]

    assert not violations, f"{violations}: backends must not depend on pickup clients"


def cast_count(path: Path) -> int:
    return sum(
        isinstance(node, ast.Call) and ast.unparse(node.func) in {"cast", "typing.cast"}
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
    )


def test_cast_count_only_falls():
    counts = {
        module_name(path): count
        for path in PACKAGE_ROOT.rglob("*.py")
        if (count := cast_count(path))
    }

    assert counts == CAST_COUNTS, (
        "cast() hides the shape of external JSON, which let T3 and Claude response changes "
        "break runs. Give the data a TypedDict checked where it enters golem instead. "
        "When the count falls, lower CAST_COUNTS to match."
    )
