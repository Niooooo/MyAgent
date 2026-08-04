import unittest

from myagent.tooling import FunctionTool, ToolRegistry
from myagent.permissions import (
    ApprovalRequest,
    BACKGROUND_BASH_TOOL,
    DefaultPermissionPolicy,
    PermissionLevel,
    PermissionManager,
    classify_bash_command,
)


class BashPermissionClassificationTests(unittest.TestCase):
    def test_safe_commands_are_allowed(self) -> None:
        for command in ["pwd", "ls -la", "printf 'rm -rf /'"]:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_bash_command(command).level,
                    PermissionLevel.ALLOW,
                )

    def test_destructive_commands_require_approval(self) -> None:
        commands = [
            "rm note.txt",
            "rmdir empty-directory",
            "mv old.txt new.txt",
            "git reset --hard HEAD~1",
            "git clean -fd",
            "git push --force origin main",
            "printf changed > note.txt",
            "sed -i 's/old/new/' note.txt",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_bash_command(command).level,
                    PermissionLevel.REQUIRE_APPROVAL,
                )

    def test_recursive_forced_rm_is_permanently_denied(self) -> None:
        commands = [
            "rm note.txt && rm -rf directory",
            "printf changed > note.txt; rm --recursive --force directory",
            "env MODE=test rm -R -f directory",
            "sudo -u root rm -rf directory",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(
                    classify_bash_command(command).level,
                    PermissionLevel.DENY,
                )


class PermissionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = DefaultPermissionPolicy()

    def test_file_reads_are_allowed_without_approval(self) -> None:
        decision = PermissionManager(self.policy).authorize(
            "read_file", {"path": "note.txt"}
        )

        self.assertEqual(decision.level, PermissionLevel.ALLOW)

    def test_sensitive_call_can_be_approved_once(self) -> None:
        requests: list[ApprovalRequest] = []
        manager = PermissionManager(
            self.policy,
            lambda request: requests.append(request) or True,
        )

        decision = manager.authorize("bash", {"command": "rm note.txt"})

        self.assertEqual(decision.level, PermissionLevel.ALLOW)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].tool_name, "bash")

    def test_missing_or_rejected_approval_is_denied(self) -> None:
        without_handler = PermissionManager(self.policy).authorize(
            "write_file", {"path": "note.txt", "content": "changed"}
        )
        rejected = PermissionManager(self.policy, lambda _: False).authorize(
            "edit_file",
            {"path": "note.txt", "old_text": "old", "new_text": "new"},
        )

        self.assertEqual(without_handler.level, PermissionLevel.DENY)
        self.assertIn("no user approval handler", without_handler.reason)
        self.assertEqual(rejected.level, PermissionLevel.DENY)
        self.assertIn("did not approve", rejected.reason)

    def test_approval_callback_failure_is_denied(self) -> None:
        def fail(_request: ApprovalRequest) -> bool:
            raise RuntimeError("approval unavailable")

        decision = PermissionManager(self.policy, fail).authorize(
            "write_file", {"path": "note.txt", "content": "changed"}
        )

        self.assertEqual(decision.level, PermissionLevel.DENY)
        self.assertIn("user approval failed", decision.reason)

    def test_permanent_denial_never_asks_for_approval(self) -> None:
        requests = []
        manager = PermissionManager(
            self.policy,
            lambda request: requests.append(request) or True,
        )

        decision = manager.authorize("bash", {"command": "rm -rf directory"})

        self.assertEqual(decision.level, PermissionLevel.DENY)
        self.assertEqual(requests, [])

    def test_background_bash_uses_the_same_command_policy(self) -> None:
        requests = []
        manager = PermissionManager(
            self.policy,
            lambda request: requests.append(request) or False,
        )

        dangerous = manager.authorize(
            BACKGROUND_BASH_TOOL,
            {"command": "rm -rf directory", "independent_work": "read docs"},
        )
        unapproved = manager.authorize(
            BACKGROUND_BASH_TOOL,
            {"command": "rm note.txt", "independent_work": "read docs"},
        )

        self.assertEqual(dangerous.level, PermissionLevel.DENY)
        self.assertEqual(unapproved.level, PermissionLevel.DENY)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].tool_name, BACKGROUND_BASH_TOOL)

    def test_non_allowlisted_tool_is_denied(self) -> None:
        decision = PermissionManager(self.policy).authorize("network", {})

        self.assertEqual(decision.level, PermissionLevel.DENY)
        self.assertIn("allowlist", decision.reason)

    def test_legacy_registry_permission_manager_injection_remains_supported(self) -> None:
        registry = ToolRegistry(
            [
                FunctionTool(
                    name="echo",
                    description="Echo",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda: {"ok": True},
                )
            ],
            permission_manager=PermissionManager(
                DefaultPermissionPolicy({"echo"})
            ),
        )

        self.assertEqual(
            [definition["name"] for definition in registry.definitions],
            ["echo"],
        )
        self.assertEqual(registry.execute("echo", "{}"), {"ok": True})


if __name__ == "__main__":
    unittest.main()
