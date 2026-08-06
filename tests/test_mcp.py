import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from myagent.agent import AgentLoop
from myagent.agent_team import AGENT_TEAM_TOOL_NAMES
from myagent.cli import _load_config
from myagent.composition import AgentConfig, build_default_components, create_default_agent
from myagent.hooks import HookRegistry, PostToolUse, PreToolUse
from myagent.permissions import BACKGROUND_BASH_TOOL, DEFAULT_TOOL_ALLOWLIST
from myagent.scheduled_tasks import SCHEDULED_TASK_TOOL_NAMES
from myagent.subagents import SUBAGENT_TOOL_NAMES
from myagent.mcp import (
    MCP_MAX_RESULT_JSON_BYTES,
    MCPRuntime,
    StdioMCPServerConfig,
    _default_client_target,
    parse_mcp_servers,
)

from tests.fakes import FakeResponses, function_call, response


class FakeClient:
    def __init__(self, pages=None, result=None, list_error=None):
        self.pages = pages or {
            None: SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="echo",
                        description="Echo input",
                        inputSchema={
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    )
                ],
                nextCursor=None,
            )
        }
        self.result = result or {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"value": 1},
            "isError": False,
        }
        self.list_error = list_error
        self.list_cursors = []
        self.calls = []

    async def list_tools(self, *, cursor=None):
        self.list_cursors.append(cursor)
        if self.list_error is not None:
            raise self.list_error
        return self.pages[cursor]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


class HangingClient(FakeClient):
    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        await asyncio.Event().wait()


class FakeTarget:
    def __init__(self, client):
        self.client = client
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self.client

    async def __aexit__(self, *_args):
        self.exited += 1


class MCPConfigTests(unittest.TestCase):
    def test_default_valid_and_duplicate_config(self):
        self.assertEqual(parse_mcp_servers([]), ())
        configs = parse_mcp_servers(
            [{
                "name": "demo",
                "transport": "stdio",
                "command": "python",
                "args": ["server.py"],
                "env": {"MODE": "test"},
            }]
        )
        self.assertEqual(configs[0].args, ("server.py",))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_mcp_servers([
                {"name": "demo", "transport": "stdio", "command": "one"},
                {"name": "demo", "transport": "stdio", "command": "two"},
            ])

    def test_cli_reports_invalid_mcp_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.json")
            path.write_text(
                json.dumps({
                    "mcp_servers": [
                        {"name": "bad", "transport": "http", "command": "x"}
                    ]
                }),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "transport must be 'stdio'"):
                _load_config(path)

    def test_null_and_non_array_values_are_rejected(self):
        for value in (None, {}, "stdio", 1, True):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "mcp_servers must be an array"
            ):
                parse_mcp_servers(value)


class MCPRuntimeTests(unittest.TestCase):
    def test_paged_discovery_call_conversion_and_close(self):
        pages = {
            None: SimpleNamespace(
                tools=[SimpleNamespace(
                    name="first", description=None,
                    input_schema={"type": "object", "properties": {}},
                )],
                next_cursor="page-2",
            ),
            "page-2": SimpleNamespace(
                tools=[SimpleNamespace(
                    name="second", description="Second",
                    input_schema={"type": "object", "properties": {}},
                )],
                next_cursor=None,
            ),
        }
        client = FakeClient(pages)
        target = FakeTarget(client)
        runtime = MCPRuntime(
            [StdioMCPServerConfig("demo", "cmd")],
            client_target_factory=lambda _config: target,
        )
        tools = runtime.start()
        self.assertEqual([tool.name for tool in tools], [
            "mcp__demo__first", "mcp__demo__second"
        ])
        result = tools[1].handler(value=7)
        self.assertTrue(result["ok"])
        self.assertEqual(result["structuredContent"], {"value": 1})
        self.assertEqual(client.calls, [("second", {"value": 7})])
        runtime.close()
        runtime.close()
        self.assertEqual(target.exited, 1)
        self.assertEqual(tools[0].handler()["code"], "mcp_closed")

    def test_partial_startup_rolls_back_entered_target(self):
        first = FakeTarget(FakeClient())
        second = FakeTarget(FakeClient(list_error=RuntimeError("broken")))
        targets = iter([first, second])
        runtime = MCPRuntime(
            [StdioMCPServerConfig("one", "cmd"), StdioMCPServerConfig("two", "cmd")],
            client_target_factory=lambda _config: next(targets),
        )
        with self.assertRaisesRegex(RuntimeError, "broken"):
            runtime.start()
        self.assertEqual(first.exited, 1)
        self.assertEqual(second.exited, 1)

    def test_timeout_is_bounded_and_sent_once(self):
        client = HangingClient()
        target = FakeTarget(client)
        runtime = MCPRuntime(
            [StdioMCPServerConfig("demo", "cmd")],
            client_target_factory=lambda _config: target,
            call_timeout_seconds=0.01,
        )
        tool = runtime.start()[0]
        try:
            self.assertEqual(tool.handler()["code"], "mcp_timeout")
            self.assertEqual(len(client.calls), 1)
        finally:
            runtime.close()

    def test_oversized_success_and_error_results_are_bounded(self):
        for remote_error in (False, True):
            client = FakeClient(result={
                "content": [{"type": "text", "text": "x" * 200_000}],
                "structuredContent": {"large": "y" * 200_000},
                "isError": remote_error,
            })
            target = FakeTarget(client)
            runtime = MCPRuntime(
                [StdioMCPServerConfig("demo", "cmd")],
                client_target_factory=lambda _config, target=target: target,
            )
            tool = runtime.start()[0]
            try:
                result = tool.handler()
                self.assertEqual(result["code"], "mcp_result_too_large")
                self.assertEqual(result["remoteIsError"], remote_error)
                self.assertLessEqual(
                    len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
                    MCP_MAX_RESULT_JSON_BYTES,
                )
            finally:
                runtime.close()

    def test_default_client_disables_input_required_rounds(self):
        constructor = MagicMock(return_value=object())
        stdio_target = object()
        modules = {
            "mcp": SimpleNamespace(
                Client=constructor,
                StdioServerParameters=MagicMock(return_value=object()),
            ),
            "mcp.client": SimpleNamespace(),
            "mcp.client.stdio": SimpleNamespace(
                stdio_client=MagicMock(return_value=stdio_target)
            ),
        }
        with patch.dict(sys.modules, modules):
            _default_client_target(StdioMCPServerConfig("demo", "cmd"))
        constructor.assert_called_once_with(
            stdio_target,
            input_required_max_rounds=0,
        )


class MCPCompositionTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.target = FakeTarget(self.client)
        self.server = StdioMCPServerConfig("demo", "cmd")

    def build(self, **kwargs):
        return build_default_components(
            bash_tool=lambda command: {"ok": True, "command": command},
            mcp_servers=(self.server,),
            mcp_client_target_factory=lambda _config: self.target,
            **kwargs,
        )

    def build_agent(self, api, root):
        components = build_default_components(
            client=SimpleNamespace(responses=api),
            cwd=root,
            bash_tool=lambda command: {"ok": True, "command": command},
            approval_callback=lambda _request: True,
            mcp_servers=(self.server,),
            mcp_client_target_factory=lambda _config: self.target,
        )
        agent = AgentLoop(
            SimpleNamespace(responses=api),
            tool_registry=components.tool_registry,
            hooks=components.hooks,
            context_memory=components.context_memory,
            background_bash_runner=components.background_bash_runner,
            scheduled_task_runtime=components.scheduled_task_runtime,
            inbox_reader=components.inbox_reader,
            close_callback=components.close,
        )
        agent.agent_team_manager = components.agent_team_manager
        return agent

    def test_default_requires_approval_and_runs_pre_post(self):
        approvals = []
        events = []
        hooks = HookRegistry()
        hooks.register(PreToolUse, lambda event: events.append(("pre", event.call_id)))
        hooks.register(PostToolUse, lambda event: events.append(("post", event.call_id)))
        components = self.build(
            approval_callback=lambda request: approvals.append(request) or True,
            hooks=hooks,
        )
        try:
            result = components.tool_registry.execute(
                "mcp__demo__echo", '{"text":"hi"}', call_id="call-x"
            )
            self.assertTrue(result["ok"])
            self.assertEqual(events, [("pre", "call-x"), ("post", "call-x")])
            self.assertIn("external MCP Server", approvals[0].reason)
        finally:
            components.close()

    def test_denial_and_explicit_allowlist_visibility(self):
        denied = self.build(approval_callback=lambda _request: False)
        try:
            result = denied.tool_registry.execute("mcp__demo__echo", "{}")
            self.assertEqual(result["code"], "permission_denied")
            self.assertEqual(self.client.calls, [])
        finally:
            denied.close()

        self.client = FakeClient()
        self.target = FakeTarget(self.client)
        explicit = self.build(allowed_tools={"read_file"})
        try:
            names = {definition["name"] for definition in explicit.tool_registry.definitions}
            self.assertNotIn("mcp__demo__echo", names)
            forged = explicit.tool_registry.execute("mcp__demo__echo", "{}")
            self.assertEqual(forged["code"], "permission_denied")
        finally:
            explicit.close()

    def test_agent_loop_preserves_call_id(self):
        api = FakeResponses([
            response([function_call("original", "mcp__demo__echo", "{}")]),
            response([], "done"),
        ])
        agent = create_default_agent(
            SimpleNamespace(responses=api),
            config=AgentConfig(mcp_servers=(self.server,)),
            approval_callback=lambda _request: True,
            mcp_client_target_factory=lambda _config: self.target,
        )
        try:
            self.assertEqual(agent.run("go"), "done")
            outputs = [
                item for item in api.requests[1]["input"]
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            ]
            self.assertEqual(len(outputs), 1)
            self.assertEqual(outputs[0]["call_id"], "original")
            self.assertNotIn(
                "mcp__demo__echo",
                agent.scheduled_task_runtime.schedulable_tool_names,
            )
        finally:
            agent.close()

    def test_no_config_adds_no_thread_or_tool(self):
        before = sum(t.name == "myagent-mcp" for t in threading.enumerate())
        components = build_default_components(
            bash_tool=lambda command: {"ok": True, "command": command}
        )
        try:
            self.assertIsNone(components.mcp_runtime)
            self.assertEqual(
                {item["name"] for item in components.tool_registry.definitions},
                DEFAULT_TOOL_ALLOWLIST.difference(
                    SUBAGENT_TOOL_NAMES
                    | SCHEDULED_TASK_TOOL_NAMES
                    | AGENT_TEAM_TOOL_NAMES
                    | {BACKGROUND_BASH_TOOL}
                ),
            )
            self.assertEqual(
                sum(t.name == "myagent-mcp" for t in threading.enumerate()),
                before,
            )
        finally:
            components.close()

    def test_composition_failure_closes_started_runtime(self):
        with self.assertRaisesRegex(ValueError, "non-empty strings"):
            self.build(allowed_tools={""})
        self.assertEqual(self.target.exited, 1)

    def test_subagent_registry_hides_and_rejects_forged_mcp_call(self):
        api = FakeResponses([
            response([function_call(
                "parent", "run_subagent", '{"task":"try mcp"}'
            )]),
            response([function_call("forged", "mcp__demo__echo", "{}")]),
            response([], "child handled"),
            response([], "parent done"),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self.build_agent(api, root)
            try:
                self.assertEqual(agent.run("delegate"), "parent done")
                child_names = {item["name"] for item in api.requests[1]["tools"]}
                self.assertNotIn("mcp__demo__echo", child_names)
                forged = json.loads(api.requests[2]["input"][-1]["output"])
                self.assertEqual(forged["code"], "unknown_tool")
                self.assertEqual(self.client.calls, [])
            finally:
                agent.close()

    def test_teammate_registry_hides_and_rejects_forged_mcp_call(self):
        api = FakeResponses([
            response([function_call("forged", "mcp__demo__echo", "{}")]),
            response([], "handled"),
        ])
        with tempfile.TemporaryDirectory() as root:
            agent = self.build_agent(api, root)
            try:
                self.assertTrue(agent.tool_registry.execute(
                    "create_teammate", '{"name":"alice","role":"tester"}'
                )["ok"])
                self.assertEqual(agent.tool_registry.execute(
                    "run_teammate", '{"name":"alice","task":"try mcp"}'
                )["status"], "running")
            finally:
                agent.close()
            member_names = {item["name"] for item in api.requests[0]["tools"]}
            self.assertNotIn("mcp__demo__echo", member_names)
            forged = json.loads(api.requests[1]["input"][-1]["output"])
            self.assertEqual(forged["code"], "unknown_tool")
            self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
