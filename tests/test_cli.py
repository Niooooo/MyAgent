import unittest
from unittest.mock import patch

from myagent.cli import _ask_user_approval
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


if __name__ == "__main__":
    unittest.main()
