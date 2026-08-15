import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from myagent.cli import (
    _CLISession,
    _ask_user_approval,
    _client_options,
    _config_from_environment,
    _load_config,
    build_parser,
    main,
)
from myagent.permissions import ApprovalRequest
from myagent.session_store import AppendTurnResult, SessionStore
from myagent.session_timeline import SessionTimeline


class ConsoleApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = ApprovalRequest(
            tool_name="bash",
            arguments={"command": "rm note.txt"},
            reason="rm deletes files or directories",
        )

    @patch("builtins.input", return_value="yes")
    @patch("builtins.print")
    def test_explicit_yes_approves(self, _print, _input) -> None:
        self.assertTrue(_ask_user_approval(self.request))

    @patch("builtins.input", return_value="")
    @patch("builtins.print")
    def test_empty_answer_denies(self, _print, _input) -> None:
        self.assertFalse(_ask_user_approval(self.request))


class EnvironmentConfigTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "AGENT_MAX_TOOL_ROUNDS": "7",
            "BASH_TIMEOUT_SECONDS": "11",
            "TODO_REMINDER_TOOL_CALLS": "3",
            "SUBAGENT_MAX_WORKERS": "2",
            "SUBAGENT_MAX_TASKS": "9",
        },
        clear=True,
    )
    def test_supported_numeric_environment_variables_are_loaded(self) -> None:
        config = _config_from_environment(
            "configured-model",
            "configured-fallback",
        )

        self.assertEqual(config.model, "configured-model")
        self.assertEqual(config.fallback_model, "configured-fallback")
        self.assertEqual(config.max_tool_rounds, 7)
        self.assertEqual(config.bash_timeout_seconds, 11)
        self.assertEqual(config.todo_reminder_tool_calls, 3)
        self.assertEqual(config.subagent_max_workers, 2)
        self.assertEqual(config.subagent_max_tasks, 9)

    @patch.dict("os.environ", {"AGENT_MAX_TOOL_ROUNDS": "invalid"}, clear=True)
    def test_invalid_numeric_environment_variable_is_reported(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Invalid numeric environment"):
            _config_from_environment("model")


class SessionArgumentTests(unittest.TestCase):
    def test_session_flags_and_positive_resume_id(self) -> None:
        self.assertTrue(build_parser().parse_args(["-c"]).continue_session)
        self.assertEqual(build_parser().parse_args(["-r", "7"]).resume, 7)
        self.assertTrue(build_parser().parse_args(["--no-session"]).no_session)
        for value in ("0", "-1", "not-an-id"):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(["--resume", value])

    def test_session_flags_are_mutually_exclusive(self) -> None:
        for arguments in (
            ["-c", "-r", "1"],
            ["-c", "--no-session"],
            ["-r", "1", "--no-session"],
        ):
            with self.assertRaises(SystemExit):
                build_parser().parse_args(arguments)


class ClientConfigurationTests(unittest.TestCase):
    def test_config_loads_client_models_and_runtime_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory, "myagent.config.json")
            expected = {
                "api_key": "file-key",
                "base_url": "https://compatible.example/v1",
                "model": "file-primary",
                "fallback_model": "file-fallback",
                "max_tool_rounds": 6,
            }
            path.write_text(json.dumps(expected), encoding="utf-8")

            self.assertEqual(_load_config(path), expected)

    def test_invalid_model_config_is_reported_before_startup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory, "myagent.config.json")
            path.write_text('{"fallback": "typo"}', encoding="utf-8")

            with self.assertRaisesRegex(SystemExit, "unsupported fields fallback"):
                _load_config(path)

            path.write_text('{"model": null}', encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "model must be a non-empty string"):
                _load_config(path)

    @patch.dict("os.environ", {}, clear=True)
    def test_file_runtime_values_feed_agent_config(self) -> None:
        config = _config_from_environment(
            "file-primary",
            "file-fallback",
            {
                "max_tool_rounds": 6,
                "bash_timeout_seconds": 12,
                "todo_reminder_tool_calls": 3,
                "subagent_max_workers": 2,
                "subagent_max_tasks": 8,
            },
        )

        self.assertEqual(config.max_tool_rounds, 6)
        self.assertEqual(config.bash_timeout_seconds, 12)
        self.assertEqual(config.todo_reminder_tool_calls, 3)
        self.assertEqual(config.subagent_max_workers, 2)
        self.assertEqual(config.subagent_max_tasks, 8)

    @patch.dict(
        "os.environ",
        {
            "OPENAI_API_KEY": "environment-key",
            "OPENAI_BASE_URL": "https://environment.example/v1",
        },
        clear=True,
    )
    def test_client_environment_overrides_file_connection_values(self) -> None:
        self.assertEqual(
            _client_options(
                {
                    "api_key": "file-key",
                    "base_url": "https://file.example/v1",
                }
            ),
            {
                "max_retries": 0,
                "api_key": "environment-key",
                "base_url": "https://environment.example/v1",
            },
        )

    @patch.dict("os.environ", {}, clear=True)
    @patch("sys.argv", ["myagent", "--no-session", "hello"])
    @patch("builtins.print")
    @patch("myagent.cli.create_default_agent")
    def test_cli_disables_sdk_retries(self, create_default_agent, _print) -> None:
        openai_constructor = MagicMock(return_value=object())
        fake_agent = MagicMock()
        fake_agent.run.return_value = "done"
        create_default_agent.return_value = fake_agent

        with patch(
            "myagent.cli._load_config",
            return_value={
                "api_key": "file-key",
                "base_url": "https://compatible.example/v1",
                "model": "file-primary",
                "fallback_model": "file-fallback",
            },
        ), patch.dict(
            "sys.modules", {"openai": SimpleNamespace(OpenAI=openai_constructor)}
        ):
            main()

        openai_constructor.assert_called_once_with(
            max_retries=0,
            api_key="file-key",
            base_url="https://compatible.example/v1",
        )
        config = create_default_agent.call_args.kwargs["config"]
        self.assertEqual(config.model, "file-primary")
        self.assertEqual(config.fallback_model, "file-fallback")
        fake_agent.run.assert_called_once_with("hello")
        fake_agent.close.assert_called_once_with()


class CLISessionCoordinatorTests(unittest.TestCase):
    def test_persist_failure_retains_every_cursor_for_retry(self) -> None:
        timeline = SessionTimeline()
        timeline.record_user_input([{"role": "user", "content": "first"}])
        store = MagicMock()
        store.append_turn.side_effect = [
            RuntimeError("disk"),
            AppendTurnResult(4, timeline.pending_operations[-1].operation_id),
        ]
        session = _CLISession(
            Path.cwd(),
            timeline,
            (),
            store=store,
            session_id=9,
            revision=3,
        )
        session.record_message("你", "first")

        with patch("builtins.print"):
            self.assertFalse(session.persist())
        self.assertEqual(session.revision, 3)
        self.assertEqual(session.message_cursor, 0)
        self.assertTrue(timeline.pending_operations)

        self.assertTrue(session.persist())
        self.assertEqual(session.revision, 4)
        self.assertEqual(session.message_cursor, 1)
        self.assertEqual(timeline.pending_operations, ())
        self.assertEqual(store.append_turn.call_args.kwargs["expected_revision"], 3)

    @patch.dict("os.environ", {}, clear=True)
    @patch("sys.argv", ["myagent", "--no-session", "hello"])
    @patch("builtins.print")
    @patch("myagent.cli.SessionStore")
    @patch("myagent.cli.create_default_agent")
    def test_no_session_never_constructs_store_and_injects_new_timeline(
        self,
        create_default_agent,
        session_store,
        _print,
    ) -> None:
        fake_agent = MagicMock()
        fake_agent.run.return_value = "done"
        create_default_agent.return_value = fake_agent
        openai_constructor = MagicMock(return_value=object())

        with patch(
            "sys.modules", {"openai": SimpleNamespace(OpenAI=openai_constructor)}
        ):
            main()

        session_store.assert_not_called()
        timeline = create_default_agent.call_args.kwargs["session_timeline"]
        self.assertIsInstance(timeline, SessionTimeline)
        self.assertEqual(timeline.operations, ())
        self.assertEqual(create_default_agent.call_args.kwargs["cwd"], Path.cwd())


if __name__ == "__main__":
    unittest.main()
