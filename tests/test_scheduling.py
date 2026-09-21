import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "kairos.py"


class SchedulingTests(unittest.TestCase):
    def test_next_reset_handles_an_early_claude_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            claude_path = bin_path / "claude"
            claude_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "early-exit",
                    "would not run",
                    "--next-reset",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("usage lookup unavailable", result.stderr)

    def test_run_at_rolls_back_when_at_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            environment = os.environ | {"PATH": str(temporary_path / "missing-bin")}

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "no-at",
                    "would not run",
                    "--at",
                    "2030-01-02T03:04:05+00:00",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not schedule task", result.stderr)
            self.assertEqual(
                json.loads((temporary_path / "tasks.json").read_text(encoding="utf-8")),
                {},
            )
            self.assertEqual(list((temporary_path / "schedules").glob("*.json")), [])

    def test_next_reset_rejects_a_malformed_control_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            claude_path = bin_path / "claude"
            claude_path.write_text(
                "#!/bin/sh\nIFS= read -r init\nprintf '%s\\n' '[]'\n",
                encoding="utf-8",
            )
            claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "malformed-response",
                    "would not run",
                    "--next-reset",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid Claude response", result.stderr)

    def test_run_rejects_an_empty_at_time_without_running_claude(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            output_path = temporary_path / "claude-called"
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            claude_path = bin_path / "claude"
            claude_path.write_text(
                "#!/bin/sh\ntouch \"$FAKE_CLAUDE_OUTPUT\"\n",
                encoding="utf-8",
            )
            claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "FAKE_CLAUDE_OUTPUT": str(output_path),
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "invalid-time",
                    "would run now",
                    "--at",
                    "",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid --at time", result.stderr)
            self.assertFalse(output_path.exists())

    def test_run_without_schedule_preserves_agent_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            output_path = temporary_path / "claude-output"
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            claude_path = bin_path / "claude"
            claude_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_CLAUDE_OUTPUT\"\n",
                encoding="utf-8",
            )
            claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "FAKE_CLAUDE_OUTPUT": str(output_path),
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "immediate-task",
                    "write the report",
                    "--",
                    "--allowed-tools",
                    "Read,Write",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output_path.read_text(encoding="utf-8").splitlines(),
                [
                    "--session-id",
                    "dfd182f0-961d-53dd-8bb6-248e6a544523",
                    "--print",
                    "write the report",
                    "--allowed-tools",
                    "Read,Write",
                ],
            )
            self.assertEqual(
                json.loads((temporary_path / "tasks.json").read_text(encoding="utf-8")),
                {"immediate-task": str(temporary_path.resolve())},
            )

    def test_run_at_registers_an_opaque_internal_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            output_path = temporary_path / "at-output"
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            at_path = bin_path / "at"
            at_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_AT_OUTPUT\"\ncat >> \"$FAKE_AT_OUTPUT\"\n",
                encoding="utf-8",
            )
            at_path.chmod(at_path.stat().st_mode | stat.S_IXUSR)
            prompt_path = temporary_path / "prompt.txt"
            prompt_path.write_text("write the report", encoding="utf-8")
            environment = os.environ | {
                "FAKE_AT_OUTPUT": str(output_path),
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
                "TZ": "UTC",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "nightly-report",
                    "--file",
                    str(prompt_path),
                    "--at",
                    "2030-01-02T03:04:05+00:00",
                    "--",
                    "--allowed-tools",
                    "Read,Write",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            submission = output_path.read_text(encoding="utf-8")
            self.assertIn("-t\n203001020305.00\n", submission)
            self.assertNotIn("write the report", submission)
            self.assertIn(
                f"{Path(sys.executable).resolve()} {script_path.resolve()} _execute ",
                submission,
            )

            tasks = json.loads((temporary_path / "tasks.json").read_text(encoding="utf-8"))
            task = tasks["nightly-report"]
            payload_path = temporary_path / "schedules" / f"{task['schedule_id']}.json"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["prompt"], "write the report")
            self.assertEqual(payload["agent_args"], ["--allowed-tools", "Read,Write"])

    def test_failed_or_duplicate_registration_keeps_existing_schedule_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            at_path = bin_path / "at"
            at_path.write_text(
                "#!/bin/sh\ncat >/dev/null\nexit \"${FAKE_AT_EXIT:-0}\"\n",
                encoding="utf-8",
            )
            at_path.chmod(at_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
                "TZ": "UTC",
            }
            command = [
                sys.executable,
                str(script_path),
                "run",
                "existing-task",
                "first prompt",
                "--at",
                "2030-01-02T03:04:05+00:00",
            ]
            original = subprocess.run(
                command,
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(original.returncode, 0, original.stderr)
            original_tasks = (temporary_path / "tasks.json").read_text(encoding="utf-8")
            original_payloads = list((temporary_path / "schedules").glob("*.json"))

            duplicate = subprocess.run(
                command,
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            failed = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "failed-task",
                    "failed prompt",
                    "--at",
                    "2030-01-02T03:04:05+00:00",
                ],
                cwd=temporary_path,
                env=environment | {"FAKE_AT_EXIT": "1"},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(duplicate.returncode, 0)
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(
                (temporary_path / "tasks.json").read_text(encoding="utf-8"),
                original_tasks,
            )
            self.assertEqual(list((temporary_path / "schedules").glob("*.json")), original_payloads)

    def test_internal_execution_claims_a_schedule_once_and_records_its_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            output_path = temporary_path / "claude-output"
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            at_path = bin_path / "at"
            at_path.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
            at_path.chmod(at_path.stat().st_mode | stat.S_IXUSR)
            claude_path = bin_path / "claude"
            claude_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$PWD\" > \"$FAKE_CLAUDE_OUTPUT\"\n"
                "printf '%s\\n' \"$CLAUDE_CODE_OAUTH_TOKEN\" >> \"$FAKE_CLAUDE_OUTPUT\"\n"
                "printf '%s\\n' \"$@\" >> \"$FAKE_CLAUDE_OUTPUT\"\nprintf 'done\\n'\n",
                encoding="utf-8",
            )
            claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "FAKE_CLAUDE_OUTPUT": str(output_path),
                "CLAUDE_CODE_OAUTH_TOKEN": "temporary-token",
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
                "TZ": "UTC",
            }
            scheduled = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "nightly-report",
                    "write the report",
                    "--at",
                    "2030-01-02T03:04:05+00:00",
                    "--",
                    "--allowed-tools",
                    "Read,Write",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(scheduled.returncode, 0, scheduled.stderr)
            task = json.loads((temporary_path / "tasks.json").read_text(encoding="utf-8"))["nightly-report"]

            first_execution = subprocess.run(
                [sys.executable, str(script_path), "_execute", task["schedule_id"]],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            second_execution = subprocess.run(
                [sys.executable, str(script_path), "_execute", task["schedule_id"]],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(first_execution.returncode, 0, first_execution.stderr)
            self.assertEqual(second_execution.returncode, 0, second_execution.stderr)
            self.assertEqual(
                output_path.read_text(encoding="utf-8").splitlines(),
                [
                    str(temporary_path.resolve()),
                    "",
                    "--session-id",
                    "f2f3e919-0e1c-55f3-bf36-341744b6be94",
                    "--print",
                    "write the report",
                    "--allowed-tools",
                    "Read,Write",
                ],
            )
            completed_task = json.loads(
                (temporary_path / "tasks.json").read_text(encoding="utf-8")
            )["nightly-report"]
            self.assertEqual(completed_task["status"], "completed")
            self.assertEqual(completed_task["returncode"], 0)
            self.assertTrue(Path(completed_task["log_path"]).is_file())

    def test_run_next_reset_uses_the_control_protocol_without_oauth_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            script_path = temporary_path / "kairos.py"
            shutil.copy(SOURCE, script_path)
            at_output_path = temporary_path / "at-output"
            usage_output_path = temporary_path / "usage-output"
            bin_path = temporary_path / "bin"
            bin_path.mkdir()
            at_path = bin_path / "at"
            at_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_AT_OUTPUT\"\ncat >> \"$FAKE_AT_OUTPUT\"\n",
                encoding="utf-8",
            )
            at_path.chmod(at_path.stat().st_mode | stat.S_IXUSR)
            claude_path = bin_path / "claude"
            claude_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$FAKE_USAGE_OUTPUT\"\n"
                "printf '%s\\n' \"$CLAUDE_CODE_OAUTH_TOKEN\" >> \"$FAKE_USAGE_OUTPUT\"\n"
                "IFS= read -r init\nprintf '%s\\n' \"$init\" >> \"$FAKE_USAGE_OUTPUT\"\n"
                "printf '%s\\n' '{\"response\":{\"subtype\":\"success\",\"request_id\":\"init\",\"response\":{}}}'\n"
                "IFS= read -r usage\nprintf '%s\\n' \"$usage\" >> \"$FAKE_USAGE_OUTPUT\"\n"
                "printf '%s\\n' '{\"response\":{\"subtype\":\"success\",\"request_id\":\"usage\",\"response\":{\"rate_limits_available\":true,\"rate_limits\":{\"five_hour\":{\"resets_at\":\"2030-01-02T03:04:05+00:00\"}}}}}'\n",
                encoding="utf-8",
            )
            claude_path.chmod(claude_path.stat().st_mode | stat.S_IXUSR)
            environment = os.environ | {
                "FAKE_AT_OUTPUT": str(at_output_path),
                "FAKE_USAGE_OUTPUT": str(usage_output_path),
                "CLAUDE_CODE_OAUTH_TOKEN": "temporary-token",
                "PATH": f"{bin_path}{os.pathsep}{os.environ['PATH']}",
                "TZ": "UTC",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "run",
                    "after-reset",
                    "write the report",
                    "--next-reset",
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("-t\n203001020305.00\n", at_output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                usage_output_path.read_text(encoding="utf-8").splitlines(),
                [
                    "-p",
                    "--model",
                    "haiku",
                    "--output-format",
                    "stream-json",
                    "--input-format",
                    "stream-json",
                    "--no-session-persistence",
                    "--verbose",
                    "",
                    '{"type":"control_request","request_id":"init","request":{"subtype":"initialize"}}',
                    '{"type":"control_request","request_id":"usage","request":{"subtype":"get_usage","skip_behaviors":true}}',
                ],
            )


if __name__ == "__main__":
    unittest.main()
