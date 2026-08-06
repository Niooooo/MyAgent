"""Long-lived stdio MCP clients adapted to MyAgent function tools."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from threading import Event, Lock, Thread, current_thread
from types import MappingProxyType
from typing import Any, AsyncContextManager

from .tooling import FunctionTool


MCP_CALL_TIMEOUT_SECONDS = 30.0
MCP_MAX_RESULT_JSON_BYTES = 65_536
_MCP_CLOSE_TIMEOUT_SECONDS = 5.0
_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_FUNCTION_NAME_LENGTH = 64
_SERVER_FIELDS = frozenset({"name", "transport", "command", "args", "env"})


@dataclass(frozen=True)
class StdioMCPServerConfig:
    """Validated, immutable configuration for one stdio MCP server."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
        compare=False,
    )
    transport: str = "stdio"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("MCP server name must be a non-empty string")
        if not _FUNCTION_NAME.fullmatch(self.name):
            raise ValueError(
                f"MCP server name {self.name!r} contains unsupported characters"
            )
        if self.transport != "stdio":
            raise ValueError("MCP server transport must be 'stdio'")
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("MCP server command must be a non-empty string")
        if not isinstance(self.args, tuple) or any(
            not isinstance(value, str) for value in self.args
        ):
            raise TypeError("MCP server args must be a string array")
        if not isinstance(self.env, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.env.items()
        ):
            raise TypeError("MCP server env must map strings to strings")
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


def parse_mcp_servers(value: object) -> tuple[StdioMCPServerConfig, ...]:
    """Parse the JSON-facing ``mcp_servers`` value into immutable configs."""
    if not isinstance(value, list):
        raise ValueError("mcp_servers must be an array")
    configs: list[StdioMCPServerConfig] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        prefix = f"mcp_servers[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        unknown = sorted(set(item).difference(_SERVER_FIELDS))
        if unknown:
            raise ValueError(f"{prefix} has unsupported fields {', '.join(unknown)}")
        missing = sorted({"name", "transport", "command"}.difference(item))
        if missing:
            raise ValueError(f"{prefix} is missing fields {', '.join(missing)}")
        args = item.get("args", [])
        env = item.get("env", {})
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            raise ValueError(f"{prefix}.args must be a string array")
        if not isinstance(env, dict) or any(
            not isinstance(key, str) or not isinstance(entry, str)
            for key, entry in env.items()
        ):
            raise ValueError(f"{prefix}.env must map strings to strings")
        try:
            config = StdioMCPServerConfig(
                name=item["name"],
                transport=item["transport"],
                command=item["command"],
                args=tuple(args),
                env=env,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{prefix}: {exc}") from exc
        if config.name in names:
            raise ValueError(f"duplicate MCP server name {config.name!r}")
        names.add(config.name)
        configs.append(config)
    return tuple(configs)


ClientTargetFactory = Callable[
    [StdioMCPServerConfig],
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
        servers: Sequence[StdioMCPServerConfig],
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
            remote_tools = self._submit(
                self._start_clients(),
                timeout=self._call_timeout_seconds,
            )
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
        try:
            result = future.result(timeout=self._call_timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            return _call_error(
                "mcp_timeout",
                f"MCP tool call timed out after {self._call_timeout_seconds:g} seconds",
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
        """Idempotently close clients, their stdio transports, and the loop thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            thread = self._thread
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._close_clients(), loop)
            try:
                future.result(timeout=_MCP_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                future.cancel()
            finally:
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread is not current_thread():
            thread.join(timeout=_MCP_CLOSE_TIMEOUT_SECONDS)

    async def _start_clients(self) -> list[_RemoteTool]:
        discovered: list[_RemoteTool] = []
        local_names: set[str] = set()
        try:
            for config in self.servers:
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

    def _submit(self, coroutine: Any, *, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._require_loop())
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("MCP startup timed out") from exc

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


def _default_client_target(config: StdioMCPServerConfig) -> AsyncContextManager[Any]:
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
