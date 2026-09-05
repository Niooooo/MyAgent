"""Validated stdio and Streamable HTTP MCP connection settings."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

_FUNCTION_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
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


@dataclass(frozen=True)
class StreamableHTTPMCPServerConfig:
    """An independent HTTP service; MyAgent owns only the connection."""

    name: str
    url: str
    connect_timeout_seconds: float = 10.0
    call_timeout_seconds: float = 120.0
    headers: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    transport: str = "streamable_http"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _FUNCTION_NAME.fullmatch(self.name):
            raise ValueError("MCP server name must contain only letters, digits, underscores or hyphens")
        if self.transport != "streamable_http":
            raise ValueError("MCP server transport must be 'streamable_http'")
        if not isinstance(self.url, str) or any(char.isspace() for char in self.url):
            raise ValueError("MCP server url must be an absolute HTTP(S) URL")
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MCP server url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError("MCP server url cannot contain credentials or a fragment")
        _ = parsed.port
        for label in ("connect_timeout_seconds", "call_timeout_seconds"):
            value = getattr(self, label)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be a finite positive number")
        if not isinstance(self.headers, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
            or "\r" in value or "\n" in value
            for key, value in self.headers.items()
        ):
            raise ValueError("MCP server headers must contain valid HTTP string headers")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


MCPServerConfig = StdioMCPServerConfig | StreamableHTTPMCPServerConfig


def parse_mcp_servers(value: object) -> tuple[MCPServerConfig, ...]:
    """Parse the JSON-facing ``mcp_servers`` value into immutable configs."""
    if not isinstance(value, list):
        raise ValueError("mcp_servers must be an array")
    configs: list[MCPServerConfig] = []
    names: set[str] = set()
    for index, item in enumerate(value):
        prefix = f"mcp_servers[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix} must be an object")
        if item.get("transport") == "streamable_http":
            allowed = {"name", "transport", "url", "headers", "connect_timeout_seconds", "call_timeout_seconds"}
            unknown = sorted(set(item).difference(allowed))
            if unknown:
                raise ValueError(f"{prefix} has unsupported fields {', '.join(unknown)}")
            try:
                config = StreamableHTTPMCPServerConfig(**item)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{prefix}: {exc}") from exc
            if config.name in names:
                raise ValueError(f"duplicate MCP server name {config.name!r}")
            names.add(config.name)
            configs.append(config)
            continue
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
