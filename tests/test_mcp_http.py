"""Network tests against the official MCP SDK, with no external model calls."""
import asyncio
import json
import socket
import threading
import time
import unittest
from typing import Any

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server import MCPServer

from myagent.composition import AgentConfig, build_default_components
from myagent.mcp import MCPRuntime, StreamableHTTPMCPServerConfig, parse_mcp_servers


class HTTPConfigTests(unittest.TestCase):
    def test_transport_config_and_composition(self):
        configs = parse_mcp_servers([
            {"name": "rag", "transport": "streamable_http", "url": "https://rag.example/mcp", "call_timeout_seconds": 90},
            {"name": "local", "transport": "stdio", "command": "python"},
        ])
        self.assertEqual(configs[0].call_timeout_seconds, 90)
        self.assertEqual(AgentConfig(mcp_servers=configs).mcp_servers, configs)
        with self.assertRaises(TypeError):
            configs[0].headers["test"] = "value"

    def test_rejects_invalid_http_settings(self):
        valid = {"name": "rag", "transport": "streamable_http", "url": "http://127.0.0.1:8080/mcp"}
        for change in (
            {"url": "file:///tmp/server"}, {"url": "http://user:secret@host/mcp"},
            {"url": "http://host:bad/mcp"}, {"url": "http://host/mcp#fragment"},
            {"call_timeout_seconds": 0}, {"call_timeout_seconds": True},
            {"connect_timeout_seconds": float("nan")}, {"connect_timeout_seconds": float("inf")},
            {"headers": {"Authorization": "x\r\ny"}}, {"headers": {"Bad Header": "x"}},
            {"command": "python"}, {"args": []},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_mcp_servers([{**valid, **change}])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_mcp_servers([valid, valid])


class HTTPRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.headers = []
        server = MCPServer("network-test")

        @server.tool(structured_output=True)
        async def ask(question: str, delay: float = 0) -> dict[str, Any]:
            cls.calls.append(question)
            await asyncio.sleep(delay)
            return {"answer": question, "sources": [{"chunk_id": "real-test-chunk"}]}

        @server.tool()
        def fail() -> str:
            raise ValueError("query unavailable")

        app = server.streamable_http_app()

        async def record_headers(request, call_next):
            cls.headers.append(request.headers.get("x-test-token"))
            return await call_next(request)

        app.add_middleware(BaseHTTPMiddleware, dispatch=record_headers)
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.url = f"http://127.0.0.1:{cls.sock.getsockname()[1]}/mcp"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + 10
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not cls.server.started:
            cls.tearDownClass()
            raise RuntimeError("Test HTTP server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.sock.close()
        if cls.thread.is_alive():
            raise AssertionError("HTTP server thread did not close")

    def config(self, **kwargs):
        return StreamableHTTPMCPServerConfig("rag", self.url, headers={"x-test-token": "test-only"}, **kwargs)

    def test_discovery_structured_result_headers_and_repeated_close(self):
        before = sum(t.name == "myagent-mcp" for t in threading.enumerate())
        for _ in range(2):
            runtime = MCPRuntime([self.config()])
            try:
                names = {tool.name for tool in runtime.start()}
                self.assertIn("mcp__rag__ask", names)
                result = runtime.call("rag", "ask", {"question": "知识库问答"})
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["structuredContent"]["answer"], "知识库问答")
                self.assertEqual(result["structuredContent"]["sources"][0]["chunk_id"], "real-test-chunk")
                self.assertTrue(runtime.call("rag", "fail", {})["isError"])
            finally:
                runtime.close()
                runtime.close()
        self.assertIn("test-only", self.headers)
        self.assertEqual(sum(t.name == "myagent-mcp" for t in threading.enumerate()), before)

    def test_approval_denial_prevents_network_tool_execution(self):
        components = build_default_components(
            mcp_servers=[self.config()], approval_callback=lambda _: False,
        )
        count = len(self.calls)
        try:
            result = components.tool_registry.execute("mcp__rag__ask", '{"question":"denied"}')
            self.assertEqual(result["code"], "permission_denied")
            self.assertEqual(len(self.calls), count)
        finally:
            components.close()

    def test_per_server_timeout_does_not_replay_query(self):
        runtime = MCPRuntime([self.config(call_timeout_seconds=0.1)])
        try:
            runtime.start()
            count = len(self.calls)
            started = time.monotonic()
            result = runtime.call("rag", "ask", {"question": "slow", "delay": 1})
            self.assertEqual(result["code"], "mcp_timeout", result)
            self.assertLess(time.monotonic() - started, 0.8)
            self.assertEqual(len(self.calls), count + 1)
        finally:
            runtime.close()

    def test_unavailable_server_fails_start_and_closes_runtime(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
        runtime = MCPRuntime([StreamableHTTPMCPServerConfig("missing", url, connect_timeout_seconds=0.2)])
        with self.assertRaises(Exception):
            runtime.start()
        runtime.close()
        self.assertEqual(runtime.call("missing", "ask", {})["code"], "mcp_closed")


if __name__ == "__main__":
    unittest.main()
