import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from myagent.agent_team import AGENT_TEAM_TOOL_NAMES

from myagent.composition import (
    AgentConfig,
    build_default_components,
    create_default_agent,
    create_default_subagent,
)
from myagent.hooks import HookRegistry, PreToolUse
from myagent.memory import MemoryConfig
from myagent.permissions import DEFAULT_TOOL_ALLOWLIST, PermissionHook, PermissionManager
from myagent.scheduled_tasks import SCHEDULED_TASK_TOOL_NAMES
from myagent.subagents import SUBAGENT_TOOL_NAMES
from myagent.worktrees import WORKTREE_TOOL_NAMES


class DefaultCompositionTests(unittest.TestCase):
    def test_components_share_hooks_and_put_permission_first(self) -> None:
        hooks = HookRegistry()
        audit = lambda event: None
        hooks.register(PreToolUse, audit)

        components = build_default_components(
            bash_tool=lambda command: {"ok": True, "command": command},
            hooks=hooks,
        )

        self.assertIs(components.hooks, hooks)
        self.assertIs(components.tool_registry.hooks, hooks)
        self.assertIsInstance(
            components.tool_registry.permission_manager,
            PermissionManager,
        )
        handlers = hooks.handlers_for(PreToolUse)
        self.assertIsInstance(handlers[0], PermissionHook)
        self.assertIs(handlers[1], audit)

    def test_default_todo_state_is_isolated_per_composition(self) -> None:
        first = build_default_components(bash_tool=lambda command: {"ok": True})
        second = build_default_components(bash_tool=lambda command: {"ok": True})

        first.todo_list.replace([{"content": "inspect", "status": "pending"}])

        self.assertEqual(len(first.todo_list.items), 1)
        self.assertEqual(second.todo_list.items, ())

    def test_agent_factory_connects_the_configured_runtime(self) -> None:
        hooks = HookRegistry()
        client = SimpleNamespace(responses=SimpleNamespace())
        memory = MemoryConfig(preview_chars=123)

        agent = create_default_agent(
            client,
            config=AgentConfig(
                model="test-model",
                fallback_model="test-fallback",
                max_tool_rounds=3,
                memory=memory,
            ),
            hooks=hooks,
        )

        self.assertIs(agent.client, client)
        self.assertEqual(agent.model, "test-model")
        self.assertEqual(agent.fallback_model, "test-fallback")
        self.assertEqual(agent.max_tool_rounds, 3)
        self.assertIs(agent.hooks, hooks)
        self.assertIs(agent.tool_registry.hooks, hooks)
        self.assertIsNotNone(agent.todo_list)
        self.assertIsNotNone(agent.task_store)
        self.assertIsNotNone(agent.context_memory)
        self.assertIs(agent.context_memory.config, memory)
        agent.close()

    def test_subagent_runtime_can_use_an_independent_client_and_model(self) -> None:
        main_client = SimpleNamespace(responses=SimpleNamespace())
        child_client = SimpleNamespace(responses=SimpleNamespace())
        child = MagicMock()
        child.run.return_value = "child result"
        with patch("myagent.composition.AgentLoop", return_value=child) as agent_loop:
            components = build_default_components(
                client=main_client,
                subagent_client=child_client,
                model="main-model",
                subagent_model="child-model",
                subagent_fallback_model="child-fallback",
            )
            self.addCleanup(components.close)
            result = components.tool_registry.execute(
                "run_subagent",
                '{"task":"inspect"}',
            )

        self.assertTrue(result["ok"])
        self.assertIs(agent_loop.call_args.args[0], child_client)
        self.assertEqual(agent_loop.call_args.kwargs["model"], "child-model")
        self.assertEqual(agent_loop.call_args.kwargs["fallback_model"], "child-fallback")

    def test_agent_factory_passes_cwd_to_every_workspace_store(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            agent = create_default_agent(
                SimpleNamespace(responses=SimpleNamespace()),
                cwd=folder,
            )
            self.addCleanup(agent.close)

            expected = Path(folder).resolve()
            self.assertEqual(agent.task_store.workspace_root, expected)
            self.assertEqual(agent.skill_store.workspace_root, expected)
            self.assertEqual(agent.long_term_memory_store.workspace_root, expected)

    def test_agent_factory_default_cwd_remains_compatible(self) -> None:
        agent = create_default_agent(SimpleNamespace(responses=SimpleNamespace()))
        self.addCleanup(agent.close)
        self.assertEqual(agent.task_store.workspace_root, Path.cwd().resolve())

    def test_standalone_subagent_isolated_tools_instructions_and_lifecycle(self) -> None:
        blocked = (
            SUBAGENT_TOOL_NAMES
            | SCHEDULED_TASK_TOOL_NAMES
            | AGENT_TEAM_TOOL_NAMES
            | WORKTREE_TOOL_NAMES
        )
        with tempfile.TemporaryDirectory() as folder:
            components = build_default_components(
                cwd=folder,
                allowed_tools=DEFAULT_TOOL_ALLOWLIST.difference(blocked),
            )
            close_callback = MagicMock()
            object.__setattr__(components, "_close_callback", close_callback)
            with patch(
                "myagent.composition.build_default_components",
                return_value=components,
            ) as build:
                agent = create_default_subagent(
                    SimpleNamespace(responses=SimpleNamespace()),
                    cwd=folder,
                )

            visible = {
                definition["name"] for definition in agent.tool_registry.definitions
            }
            selected = frozenset(build.call_args.kwargs["allowed_tools"])
            self.assertFalse(visible & blocked)
            self.assertFalse(selected & blocked)
            forged = agent.tool_registry.execute(
                "fork_subagent",
                '{"task":"not allowed"}',
            )
            self.assertEqual(forged["code"], "unknown_tool")
            self.assertIn("isolated sub-agent", agent.instructions)
            self.assertIsNone(agent.agent_team_manager)
            self.assertIsInstance(
                agent.hooks.handlers_for(PreToolUse)[0],
                PermissionHook,
            )
            agent.close()
            agent.close()
            close_callback.assert_called_once_with()

    def test_agent_config_allowlist_can_disable_subagent_tools(self) -> None:
        agent = create_default_agent(
            SimpleNamespace(responses=SimpleNamespace()),
            config=AgentConfig(allowed_tools=frozenset({"read_file"})),
        )
        self.addCleanup(agent.close)

        visible = {
            definition["name"] for definition in agent.tool_registry.definitions
        }
        forged = agent.tool_registry.execute(
            "fork_subagent",
            '{"task":"not allowed"}',
        )
        forged_background = agent.tool_registry.execute(
            "run_bash_in_background",
            '{"command":"sleep 1","independent_work":"read a file"}',
        )

        self.assertEqual(visible, {"read_file"})
        self.assertEqual(forged["permission"], "denied")
        self.assertEqual(forged_background["permission"], "denied")

    def test_background_bash_is_main_only_guarded_and_closed_with_agent(self) -> None:
        seen_commands = []
        components = build_default_components(
            client=SimpleNamespace(responses=SimpleNamespace()),
            bash_tool=lambda command: seen_commands.append(command) or {"ok": True},
        )
        self.addCleanup(components.close)
        registry = components.tool_registry

        visible = {definition["name"] for definition in registry.definitions}
        invalid = registry.execute(
            "run_bash_in_background",
            '{"command":"sleep 1","independent_work":"  "}',
        )
        dangerous = registry.execute(
            "run_bash_in_background",
            '{"command":"rm -rf directory","independent_work":"read docs"}',
        )
        unapproved = registry.execute(
            "run_bash_in_background",
            '{"command":"rm note.txt","independent_work":"read docs"}',
        )

        self.assertIn("run_bash_in_background", visible)
        self.assertEqual(invalid["code"], "invalid_independent_work")
        self.assertEqual(dangerous["permission"], "denied")
        self.assertEqual(unapproved["permission"], "denied")
        self.assertEqual(seen_commands, [])
        self.assertIsNotNone(components.background_bash_runner)
        self.assertFalse(components.background_bash_runner.has_pending())

        components.close()
        closed = registry.execute(
            "run_bash_in_background",
            '{"command":"sleep 1","independent_work":"read docs"}',
        )
        self.assertEqual(closed["code"], "runner_closed")

    def test_agent_close_shuts_down_subagent_management(self) -> None:
        agent = create_default_agent(
            SimpleNamespace(responses=SimpleNamespace()),
        )
        registry = agent.tool_registry

        agent.close()
        result = registry.execute("fork_subagent", '{"task":"too late"}')

        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["code"], "manager_closed")


if __name__ == "__main__":
    unittest.main()
