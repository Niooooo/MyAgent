import unittest
from unittest.mock import patch

from myagent.cli import _ask_user_approval, _config_from_environment
from myagent.permissions import ApprovalRequest


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
        },
        clear=True,
    )
    def test_supported_numeric_environment_variables_are_loaded(self) -> None:
        config = _config_from_environment("configured-model")

        self.assertEqual(config.model, "configured-model")
        self.assertEqual(config.max_tool_rounds, 7)
        self.assertEqual(config.bash_timeout_seconds, 11)
        self.assertEqual(config.todo_reminder_tool_calls, 3)

    @patch.dict("os.environ", {"AGENT_MAX_TOOL_ROUNDS": "invalid"}, clear=True)
    def test_invalid_numeric_environment_variable_is_reported(self) -> None:
        with self.assertRaisesRegex(SystemExit, "Invalid numeric environment"):
            _config_from_environment("model")


if __name__ == "__main__":
    unittest.main()
