import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from myagent.composition import build_default_components
from myagent.hooks import HookRegistry, PostToolUse, PreToolUse
from myagent.tasks import TaskStore
from myagent.worktrees import WorktreeManager


@unittest.skipUnless(shutil.which("git"), "git is required")
class WorktreeManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.repository = Path(self.temporary_directory.name) / "repo"
        self.repository.mkdir()
        self.git("init")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        (self.repository / "tracked.txt").write_text("base", encoding="utf-8")
        self.git("add", "tracked.txt")
        self.git("commit", "-m", "initial")
        self.store = TaskStore(self.repository)
        self.manager = WorktreeManager(self.repository, self.store)

    def tearDown(self) -> None:
        if hasattr(self, "repository") and self.repository.exists():
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.repository,
                capture_output=True,
                check=False,
            )

    def git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=self.repository,
            capture_output=True,
            text=True,
            check=True,
        )

    def create_task(self) -> dict[str, object]:
        return self.store.create_task("Task", "summary", "test", [])

    def test_create_and_clean_delete_preserve_task_lifecycle(self) -> None:
        task = self.create_task()

        created = self.manager.create(task["id"])

        self.assertTrue(created["ok"])
        worktree = Path(created["worktree"])
        self.assertTrue(worktree.is_dir())
        self.assertEqual(created["status"], "pending")
        self.assertIsNone(created["owner"])
        self.assertIn(
            worktree.as_posix(), self.git("worktree", "list", "--porcelain").stdout
        )

        deleted = self.manager.delete(task["id"])
        self.assertTrue(deleted["ok"])
        self.assertFalse(worktree.exists())
        self.assertIsNone(self.store.get_task(task["id"])["worktree"])

    def test_dirty_and_in_progress_delete_keep_binding(self) -> None:
        task = self.create_task()
        created = self.manager.create(task["id"])
        worktree = Path(created["worktree"])
        (worktree / "dirty.txt").write_text("dirty", encoding="utf-8")

        dirty = self.manager.delete(task["id"])
        self.assertFalse(dirty["ok"])
        self.assertEqual(dirty["code"], "git_command_failed")
        self.assertTrue((worktree / "dirty.txt").exists())
        self.assertEqual(self.store.get_task(task["id"])["worktree"], str(worktree))

        self.store.claim_task(task["id"], "alice")
        running = self.manager.delete(task["id"])
        self.assertFalse(running["ok"])
        self.assertEqual(running["code"], "task_in_progress")
        self.assertTrue(worktree.exists())

    def test_registry_requires_each_approval_and_preserves_hooks_and_call_id(self) -> None:
        approvals = iter([False, True, True])
        approval_names: list[str] = []
        pre: list[tuple[str, str | None, str | None]] = []
        post: list[tuple[str, str | None]] = []
        hooks = HookRegistry()
        hooks.register(
            PreToolUse,
            lambda event: pre.append(
                (event.tool_name, event.call_id, event.permission_level)
            ),
        )
        hooks.register(
            PostToolUse,
            lambda event: post.append((event.tool_name, event.call_id)),
        )

        def approve(request):
            approval_names.append(request.tool_name)
            return next(approvals)

        components = build_default_components(
            cwd=self.repository,
            task_store=self.store,
            approval_callback=approve,
            hooks=hooks,
        )
        task = self.create_task()
        raw = '{"id":"' + task["id"] + '"}'

        denied = components.tool_registry.execute(
            "create_worktree", raw, call_id="denied"
        )
        self.assertEqual(denied["code"], "permission_denied")
        self.assertIsNone(self.store.get_task(task["id"])["worktree"])
        created = components.tool_registry.execute(
            "create_worktree", raw, call_id="create"
        )
        deleted = components.tool_registry.execute(
            "delete_worktree", raw, call_id="delete"
        )

        self.assertTrue(created["ok"])
        self.assertTrue(deleted["ok"])
        self.assertEqual(
            approval_names,
            ["create_worktree", "create_worktree", "delete_worktree"],
        )
        self.assertEqual(
            pre,
            [
                ("create_worktree", "denied", "deny"),
                ("create_worktree", "create", "allow"),
                ("delete_worktree", "delete", "allow"),
            ],
        )
        self.assertEqual(
            post, [("create_worktree", "create"), ("delete_worktree", "delete")]
        )
