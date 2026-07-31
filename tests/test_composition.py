import unittest
from types import SimpleNamespace

from myagent.composition import (
    AgentConfig,
    build_default_components,
    create_default_agent,
)
from myagent.hooks import HookRegistry, PreToolUse
from myagent.permissions import PermissionHook, PermissionManager


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

        agent = create_default_agent(
            client,
            config=AgentConfig(model="test-model", max_tool_rounds=3),
            hooks=hooks,
        )

        self.assertIs(agent.client, client)
        self.assertEqual(agent.model, "test-model")
        self.assertEqual(agent.max_tool_rounds, 3)
        self.assertIs(agent.hooks, hooks)
        self.assertIs(agent.tool_registry.hooks, hooks)
        self.assertIsNotNone(agent.todo_list)


if __name__ == "__main__":
    unittest.main()
