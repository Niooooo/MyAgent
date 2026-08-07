import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from myagent.tools import (
    BackgroundBashRunner,
    BashTool,
    _resolve_bash_executable,
    build_default_tool_registry,
    is_dangerous_command,
)


class DangerousCommandTests(unittest.TestCase):
    def test_blocks_common_recursive_force_variants(self) -> None:
        dangerous = [
            "rm -rf /tmp/example",
            "rm -fr /tmp/example",
            "rm -r -f /tmp/example",
            "rm --recursive --force /tmp/example",
            "echo safe && sudo rm -R -f /tmp/example",
            "/usr/bin/rm -rf /tmp/example",
            "echo safe\nrm -rf /tmp/example",
        ]
        for command in dangerous:
            with self.subTest(command=command):
                self.assertTrue(is_dangerous_command(command))

    def test_allows_non_matching_commands(self) -> None:
        safe_for_current_policy = [
            "ls -la",
            "printf 'rm -rf /'",
            "rm file.txt",
            "rm -r directory",
            "rm -f file.txt",
            "echo rm -rf /tmp/example",
        ]
        for command in safe_for_current_policy:
            with self.subTest(command=command):
                self.assertFalse(is_dangerous_command(command))


class BashToolTests(unittest.TestCase):
    @patch("myagent.tools.subprocess.Popen")
    def test_refusal_happens_before_subprocess(self, popen) -> None:
        result = BashTool()("rm -rf /tmp/example")

        self.assertFalse(result["ok"])
        self.assertIn("refused", result["error"])
        popen.assert_not_called()

    def test_windows_prefers_git_bash_over_the_system_wsl_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            git_root = Path(temporary_directory) / "Git"
            git_executable = git_root / "cmd" / "git.exe"
            bash_executable = git_root / "bin" / "bash.exe"
            direct_bash_executable = git_root / "usr" / "bin" / "bash.exe"
            git_executable.parent.mkdir(parents=True)
            bash_executable.parent.mkdir(parents=True)
            direct_bash_executable.parent.mkdir(parents=True)
            git_executable.touch()
            bash_executable.touch()
            direct_bash_executable.touch()

            def which(name: str) -> str | None:
                if name == "git":
                    return str(git_executable)
                if name == "bash":
                    return r"C:\Windows\System32\bash.exe"
                return None

            with patch.dict(os.environ, {}, clear=True), patch(
                "myagent.tools.sys.platform",
                "win32",
            ), patch("myagent.tools.shutil.which", side_effect=which):
                selected = _resolve_bash_executable()

        self.assertEqual(selected, str(direct_bash_executable))

    def test_explicit_bash_override_wins(self) -> None:
        with patch.dict(
            os.environ,
            {"MYAGENT_BASH": r"D:\PortableGit\bin\bash.exe"},
            clear=True,
        ):
            self.assertEqual(
                _resolve_bash_executable(),
                r"D:\PortableGit\bin\bash.exe",
            )

    @patch("myagent.tools._WindowsProcessJob.try_create", return_value=None)
    @patch("myagent.tools.subprocess.Popen")
    def test_none_process_output_is_normalized_to_empty_text(
        self,
        popen,
        _try_create_job,
    ) -> None:
        process = popen.return_value
        process.communicate.return_value = (None, None)
        process.returncode = 0
        executable = r"C:\Program Files\Git\bin\bash.exe"

        result = BashTool(bash_executable=executable)("printf hello")

        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "")
        self.assertEqual(result["stderr"], "")
        self.assertIsNone(result["error"])
        self.assertEqual(
            popen.call_args.args[0],
            [
                executable,
                "--noprofile",
                "--norc",
                "-c",
                "printf hello",
            ],
        )
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["encoding"], "utf-8")
        process.communicate.assert_called_once_with(timeout=30)

    @patch("myagent.tools._WindowsProcessJob.try_create", return_value=None)
    @patch("myagent.tools.subprocess.Popen")
    def test_nonzero_exit_has_a_clear_error(
        self,
        popen,
        _try_create_job,
    ) -> None:
        process = popen.return_value
        process.communicate.return_value = ("", "failed")
        process.returncode = 7

        result = BashTool(bash_executable="bash")("exit 7")

        self.assertFalse(result["ok"])
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(result["stderr"], "failed")
        self.assertEqual(result["error"], "Command exited with code 7.")

    @patch("myagent.tools._WindowsProcessJob.try_create")
    @patch("myagent.tools.subprocess.Popen")
    def test_timeout_terminates_the_owned_process_tree(
        self,
        popen,
        try_create_job,
    ) -> None:
        process = popen.return_value
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(
                cmd=["bash", "-lc", "slow command"],
                timeout=1,
                output="partial output",
            ),
            ("partial output", ""),
        ]
        job = MagicMock()
        job.terminate.return_value = None
        try_create_job.return_value = job

        result = BashTool(
            bash_executable="bash",
            timeout_seconds=1,
        )("slow command")

        self.assertFalse(result["ok"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["stdout"], "partial output")
        self.assertEqual(result["error"], "Command timed out after 1 seconds.")
        job.terminate.assert_called_once_with()
        job.close.assert_called_once_with()
        self.assertEqual(process.communicate.call_count, 2)

    def test_registry_rejects_empty_command_before_injected_handler(self) -> None:
        seen_commands = []
        registry = build_default_tool_registry(
            bash_tool=lambda command: seen_commands.append(command) or {"ok": True}
        )

        result = registry.execute("bash", '{"command": "  "}')

        self.assertFalse(result["ok"])
        self.assertIn("non-empty", result["error"])
        self.assertEqual(seen_commands, [])

    def test_allowlist_hides_and_blocks_unlisted_tools(self) -> None:
        seen_commands = []
        registry = build_default_tool_registry(
            bash_tool=lambda command: seen_commands.append(command) or {"ok": True},
            allowed_tools={"read_file", "glob", "grep"},
        )

        visible_names = {definition["name"] for definition in registry.definitions}
        result = registry.execute("bash", '{"command": "pwd"}')

        self.assertEqual(visible_names, {"read_file", "glob", "grep"})
        self.assertFalse(result["ok"])
        self.assertIn("allowlist", result["error"])
        self.assertEqual(seen_commands, [])

    def test_sensitive_command_only_executes_after_user_approval(self) -> None:
        seen_commands = []
        requests = []
        registry = build_default_tool_registry(
            bash_tool=lambda command: seen_commands.append(command) or {"ok": True},
            approval_callback=lambda request: requests.append(request) or True,
        )

        result = registry.execute("bash", '{"command": "rm note.txt"}')

        self.assertTrue(result["ok"])
        self.assertEqual(seen_commands, ["rm note.txt"])
        self.assertEqual(len(requests), 1)

    def test_denied_sensitive_command_does_not_reach_handler(self) -> None:
        seen_commands = []
        registry = build_default_tool_registry(
            bash_tool=lambda command: seen_commands.append(command) or {"ok": True}
        )

        result = registry.execute("bash", '{"command": "rm note.txt"}')

        self.assertFalse(result["ok"])
        self.assertEqual(result["permission"], "denied")
        self.assertEqual(seen_commands, [])

    def test_write_file_requires_user_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            registry = build_default_tool_registry(cwd=temporary_directory)

            result = registry.execute(
                "write_file",
                '{"path": "note.txt", "content": "changed"}',
            )

        self.assertFalse(result["ok"])
        self.assertIn("approval", result["error"])


class BackgroundBashRunnerTests(unittest.TestCase):
    def test_submit_returns_while_handler_is_blocked(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def handler(command: str) -> dict[str, object]:
            started.set()
            release.wait(2)
            return {"ok": True, "command": command}

        runner = BackgroundBashRunner(handler)
        try:
            submitted = runner.submit("slow")
            self.assertTrue(submitted["ok"])
            self.assertTrue(started.wait(1))
            self.assertEqual(runner.drain_completed(), [])
        finally:
            release.set()
            runner.close()

    def test_drain_batches_completed_tasks_in_submission_order(self) -> None:
        runner = BackgroundBashRunner(
            lambda command: {"ok": True, "command": command}
        )
        self.addCleanup(runner.close)
        first = runner.submit("first")
        second = runner.submit("second")

        runner.wait_for_all()
        results = runner.drain_completed()

        self.assertEqual(
            [result["background_task_id"] for result in results],
            [first["background_task_id"], second["background_task_id"]],
        )
        self.assertEqual(runner.drain_completed(), [])

    def test_task_limit_and_close_are_stable(self) -> None:
        release = threading.Event()
        runner = BackgroundBashRunner(
            lambda _command: release.wait(2) or {"ok": True},
            max_workers=1,
            max_tasks=1,
        )
        self.assertTrue(runner.submit("first")["ok"])
        self.assertEqual(runner.submit("second")["code"], "task_limit_reached")

        release.set()
        runner.close()
        runner.close()
        self.assertEqual(runner.submit("late")["code"], "runner_closed")

    def test_registry_only_compatibility_entry_has_no_background_tool(self) -> None:
        registry = build_default_tool_registry(
            bash_tool=lambda command: {"ok": True, "command": command}
        )

        self.assertNotIn(
            "run_bash_in_background",
            {definition["name"] for definition in registry.definitions},
        )


if __name__ == "__main__":
    unittest.main()
