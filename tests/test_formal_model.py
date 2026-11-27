import ast
import hashlib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parent.parent / "golem"
COVERED_FUNCTION_HASHES = {
    "backends/t3.py": {
        "has_active_run": "fab6d30a129e3d32",
        "has_unfinished_subagent_work": "47113c5b098646e1",
        "wait_for_quiet_thread": "384b357d15e09e53",
    },
    "t3_code.py": {
        "is_cancelled_before_start": "dcfdca565f968506",
        "is_thread_settleable": "5138b734c142c4ca",
        "latest_run": "62f6b95c0062d043",
        "latest_run_not_cancelled_before_start": "1a65151ba90d96f0",
        "settle_idle_thread": "aab570c9aea71a67",
    },
}


def function_hashes(relative_path: str, names: set[str]) -> dict[str, str]:
    source = (PACKAGE_ROOT / relative_path).read_text(encoding="utf-8")
    return {
        node.name: hashlib.sha256(ast.dump(node).encode()).hexdigest()[:16]
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }


def test_formal_model_matches_code():
    hashes = {
        relative_path: function_hashes(relative_path, set(expected))
        for relative_path, expected in COVERED_FUNCTION_HASHES.items()
    }

    assert hashes == COVERED_FUNCTION_HASHES, (
        "code covered by formal/T3ThreadWait.tla changed: follow "
        ".claude/skills/verify-t3-thread-wait/SKILL.md, then update COVERED_FUNCTION_HASHES"
    )
