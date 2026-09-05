"""Long-lived MCP clients adapted to MyAgent function tools."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from contextlib import asynccontextmanager
from dataclasses import dataclass
from threading import Event, Lock, Thread, current_thread
from typing import Any, AsyncContextManager

from .tooling import FunctionTool
from .mcp_config import (
    MCPServerConfig,
    StdioMCPServerConfig,
    StreamableHTTPMCPServerConfig,
    parse_mcp_servers,
)


MCP_CALL_TIMEOUT_SECONDS = 30.0
MCP_MAX_RESULT_JSON_BYTES = 65_536
_MCP_CLOSE_TIMEOUT_SECONDS = 5.0
_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_FUNCTION_NAME_LENGTH = 64


ClientTargetFactory = Callable[
    [MCPServerConfig],
    AsyncContextManager[Any],
]


@dataclass(frozen=True)
class _RemoteTool:
    server: str
    remote_name: str
    local_name: str
    description: str
    parameters: dict[str, Any]


class MCPRuntime:
    """Own one event-loop thread and long-lived clients for configured servers."""

    def __init__(
        self,
        servers: Sequence[MCPServerConfig],
        *,
        client_target_factory: ClientTargetFactory | None = None,
        call_timeout_seconds: float = MCP_CALL_TIMEOUT_SECONDS,
    ) -> None:
        if not servers:
            raise ValueError("MCPRuntime requires at least one server")
        if call_timeout_seconds <= 0:
            raise ValueError("call_timeout_seconds must be positive")
        self.servers = tuple(servers)
        self._factory = client_target_factory or _default_client_target
        self._call_timeout_seconds = call_timeout_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._ready = Event()
        self._lock = Lock()
        self._closed = False
        self._started = False
        self._clients: dict[str, Any] = {}
        self._targets: list[AsyncContextManager[Any]] = []
        self._configs = {server.name: server for server in servers}
        self._startup: Future[list[_RemoteTool]] = Future()
        self._lifecycle: Future[None] | None = None
        self._stop_clients: asyncio.Event | None = None

    def start(self) -> tuple[FunctionTool, ...]:
        """Connect once, discover every tools page, and return local adapters."""
        with self._lock:
            if self._closed:
                raise RuntimeError("MCP runtime is closed")
            if self._started:
                raise RuntimeError("MCP runtime is already started")
            self._started = True
            self._thread = Thread(
                target=self._run_loop,
                name="myagent-mcp",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(_MCP_CLOSE_TIMEOUT_SECONDS):
            self.close()
            raise RuntimeError("MCP event loop did not start")
        try:
            self._lifecycle = asyncio.run_coroutine_threadsafe(self._manage_clients(), self._require_loop())
            startup_timeout = sum(
                config.connect_timeout_seconds if isinstance(config, StreamableHTTPMCPServerConfig)
                else self._call_timeout_seconds for config in self.servers
            )
            remote_tools = self._startup.result(timeout=startup_timeout + 1)
            return tuple(self._adapt_tool(tool) for tool in remote_tools)
        except BaseException:
            self.close()
            raise

    def call(
        self,
        server: str,
        remote_tool: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Invoke a remote tool once and convert its result to bounded JSON data."""
        with self._lock:
            if self._closed:
                return _call_error(
                    "mcp_closed",
                    "MCP runtime is closed",
                    server,
                    remote_tool,
                )
            client = self._clients.get(server)
        if client is None:
            return _call_error(
                "mcp_unavailable",
                "MCP server is not connected",
                server,
                remote_tool,
            )
        future = asyncio.run_coroutine_threadsafe(
            client.call_tool(remote_tool, dict(arguments)),
            self._require_loop(),
        )
        config = self._configs[server]
        timeout = config.call_timeout_seconds if isinstance(config, StreamableHTTPMCPServerConfig) else self._call_timeout_seconds
        try:
            result = future.result(timeout=timeout)
        except FutureTimeoutError:
            future.cancel()
            return _call_error(
                "mcp_timeout",
                f"MCP tool call timed out after {timeout:g} seconds",
                server,
                remote_tool,
            )
        except Exception as exc:
            return _call_error(
                "mcp_call_failed",
                _bounded_error(exc),
                server,
                remote_tool,
            )
        return _convert_call_result(result, server, remote_tool)

    def close(self) -> None:
        """Close each transport in the same task that entered its context."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None and loop.is_running():
            if self._stop_clients is not None:
                loop.call_soon_threadsafe(self._stop_clients.set)
            try:
                if self._lifecycle is not None:
                    self._lifecycle.result(timeout=_MCP_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                if self._lifecycle is not None:
                    self._lifecycle.cancel()
            finally:
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not current_thread():
            thread.join(timeout=_MCP_CLOSE_TIMEOUT_SECONDS)

    async def _manage_clients(self) -> None:
        # SDK transports own AnyIO cancel scopes; enter and exit on one task.
        self._stop_clients = asyncio.Event()
        try:
            discovered = await self._start_clients()
            self._startup.set_result(discovered)
            await self._stop_clients.wait()
        except BaseException as exc:
            if not self._startup.done():
                self._startup.set_exception(exc)
            raise
        finally:
            await self._close_clients()

    async def _start_clients(self) -> list[_RemoteTool]:
        discovered: list[_RemoteTool] = []
        local_names: set[str] = set()
        try:
            for config in self.servers:
                timeout = config.connect_timeout_seconds if isinstance(config, StreamableHTTPMCPServerConfig) else self._call_timeout_seconds
                async with asyncio.timeout(timeout):
                    target = self._factory(config)
                    client = await target.__aenter__()
                    self._targets.append(target)
                    self._clients[config.name] = client
                    cursor: str | None = None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        for raw_tool in _page_tools(page):
                            tool = _validate_remote_tool(config.name, raw_tool)
                            if tool.local_name in local_names:
                                raise ValueError(
                                    f"Conflicting MCP tool name {tool.local_name!r}"
                                )
                            local_names.add(tool.local_name)
                            discovered.append(tool)
                        cursor = _page_cursor(page)
                        if cursor is None:
                            break
        except BaseException:
            await self._close_clients()
            raise
        return discovered

    async def _close_clients(self) -> None:
        targets, self._targets = self._targets, []
        self._clients.clear()
        first_error: BaseException | None = None
        for target in reversed(targets):
            try:
                await target.__aexit__(None, None, None)
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _adapt_tool(self, tool: _RemoteTool) -> FunctionTool:
        def handler(**arguments: Any) -> dict[str, Any]:
            return self.call(tool.server, tool.remote_name, arguments)

        return FunctionTool(
            name=tool.local_name,
            description=tool.description,
            parameters=tool.parameters,
            handler=handler,
            strict=False,
        )

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None:
            raise RuntimeError("MCP event loop is unavailable")
        return loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()


def _default_client_target(config: MCPServerConfig) -> AsyncContextManager[Any]:
    if isinstance(config, StreamableHTTPMCPServerConfig):
        return _http_client_target(config)
    # The optional SDK is deliberately imported only for a configured runtime.
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=dict(config.env) or None,
    )
    return Client(
        stdio_client(parameters),
        input_required_max_rounds=0,
    )


@asynccontextmanager
async def _http_client_target(config: StreamableHTTPMCPServerConfig):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(
        headers=dict(config.headers),
        timeout=httpx2.Timeout(config.call_timeout_seconds + 1, connect=config.connect_timeout_seconds),
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        async with Client(
            streamable_http_client(config.url, http_client=http_client),
            read_timeout_seconds=config.call_timeout_seconds + 1,
            input_required_max_rounds=0,
        ) as client:
            yield client


def _page_tools(page: object) -> Sequence[object]:
    tools = page.get("tools") if isinstance(page, Mapping) else getattr(page, "tools", None)
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        raise ValueError("MCP tools/list response has no tools array")
    return tools


def _page_cursor(page: object) -> str | None:
    if isinstance(page, Mapping):
        value = page.get("nextCursor", page.get("next_cursor"))
    else:
        value = getattr(page, "nextCursor", getattr(page, "next_cursor", None))
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("MCP tools/list returned an invalid cursor")
    return value


def _validate_remote_tool(server: str, raw_tool: object) -> _RemoteTool:
    name = _field(raw_tool, "name")
    if not isinstance(name, str) or not _FUNCTION_NAME.fullmatch(name):
        raise ValueError(f"MCP server {server!r} returned an invalid tool name")
    local_name = f"mcp__{server}__{name}"
    if len(local_name) > _MAX_FUNCTION_NAME_LENGTH:
        raise ValueError(f"MCP tool name {local_name!r} exceeds 64 characters")
    schema = _field(raw_tool, "input_schema", "inputSchema")
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        raise ValueError(f"MCP tool {server}/{name} requires an object input schema")
    parameters = dict(schema)
    try:
        json.dumps(parameters, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCP tool {server}/{name} has a non-JSON schema") from exc
    description = _field(raw_tool, "description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"MCP tool {server}/{name} has an invalid description")
    source = f"External MCP server {server!r}, tool {name!r}."
    if description and description.strip():
        source = f"{source} {description.strip()}"
    return _RemoteTool(server, name, local_name, source, parameters)


def _field(value: object, *names: str) -> object:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _convert_call_result(result: object, server: str, remote_tool: str) -> dict[str, Any]:
    content = _json_value(_field(result, "content"))
    structured = _json_value(_field(result, "structuredContent", "structured_content"))
    is_error = bool(_field(result, "isError", "is_error"))
    converted = {
        "ok": not is_error,
        "server": server,
        "remoteTool": remote_tool,
        "content": content,
        "isError": is_error,
    }
    if structured is not None:
        converted["structuredContent"] = structured
    try:
        encoded = json.dumps(
            converted,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return _call_error(
            "mcp_invalid_result",
            f"MCP tool returned a non-JSON result: {_bounded_error(exc)}",
            server,
            remote_tool,
        )
    if len(encoded) > MCP_MAX_RESULT_JSON_BYTES:
        bounded = _call_error(
            "mcp_result_too_large",
            f"MCP tool result exceeded {MCP_MAX_RESULT_JSON_BYTES} JSON bytes",
            server,
            remote_tool,
        )
        bounded["remoteIsError"] = is_error
        return bounded
    return converted


def _json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json", by_alias=True))
    return str(value)


def _call_error(code: str, message: str, server: str, remote_tool: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "error": message[:500],
        "server": server,
        "remoteTool": remote_tool,
        "content": [],
        "isError": True,
    }


def _bounded_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:500]
