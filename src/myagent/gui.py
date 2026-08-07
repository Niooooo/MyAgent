"""Dark, multi-workspace Tkinter desktop UI for MyAgent."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from .agent import AgentLoop, AgentLoopLimitError, StreamCallback
from .cli import _client_options, _config_from_environment, _load_config
from .composition import AgentConfig, create_default_agent, create_default_subagent
from .gui_conversations import ConversationStore, ConversationStoreError, SCHEMA_VERSION
from .gui_models import (
    DesktopSettings,
    DesktopSettingsError,
    DesktopSettingsStore,
    ModelRecord,
    ModelStore,
    ModelStoreError,
)
from .permissions import ApprovalCallback, ApprovalRequest


EventKind = Literal["assistant", "error", "approval", "stream_start", "stream_delta"]
AgentKind = Literal["main", "sub"]
RuntimeFactory = Callable[..., AgentLoop]

MAIN_AGENT_LABEL = "主 Agent（含 Agent Teammate）"
SUBAGENT_LABEL = "子 Agent"
AGENT_KIND_LABELS: dict[AgentKind, str] = {
    "main": MAIN_AGENT_LABEL,
    "sub": SUBAGENT_LABEL,
}
LABEL_AGENT_KINDS = {label: kind for kind, label in AGENT_KIND_LABELS.items()}

_APP_BACKGROUND = "#0b0e10"
_HEADER_BACKGROUND = "#0f1315"
_PANEL_BACKGROUND = "#121719"
_INPUT_BACKGROUND = "#171d20"
_RAISED_BACKGROUND = "#20282c"
_ACTIVE_BACKGROUND = "#16223a"
_BORDER_COLOR = "#2a3439"
_TEXT_COLOR = "#f1f5f3"
_MUTED_TEXT_COLOR = "#8f9ca2"
_ACCENT_COLOR = "#275dce"
_ACCENT_HOVER_COLOR = "#356de0"
_ACCENT_DISABLED_COLOR = "#2d3947"
_ACCENT_TEXT_COLOR = "#f7f9ff"
_SIGNAL_MUTED_COLOR = "#527ec4"
_ERROR_COLOR = "#ff7b7b"
_DANGER_HOVER_COLOR = "#3a2023"
_FONT_FAMILY = "Microsoft YaHei UI"
_MONO_FONT_FAMILY = "Cascadia Mono"
_FONT_SIZE_CAPTION = 10
_FONT_SIZE_CONTROL = 11
_FONT_SIZE_BODY = 12
_FONT_SIZE_CARD_TITLE = 14
_FONT_SIZE_BRAND = 16
_FONT_SIZE_DIALOG_TITLE = 20
_FONT_SIZE_PAGE_TITLE = 22
_COMPACT_ENTER_WIDTH = 880
_COMPACT_EXIT_WIDTH = 920
_COMPACT_INITIAL_WIDTH = (_COMPACT_ENTER_WIDTH + _COMPACT_EXIT_WIDTH) / 2
_WINDOWS_APP_ID = "Niooooo.MyAgent.Desktop"

ctk.set_appearance_mode("dark")


def _set_windows_app_id() -> bool:
    """Give the pythonw-hosted window its own Windows taskbar identity."""
    if os.name != "nt":
        return False
    try:
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        return setter(_WINDOWS_APP_ID) == 0
    except (AttributeError, OSError):
        return False


@dataclass(frozen=True)
class UIEvent:
    """A plain value produced by a worker and consumed by Tk's main thread."""

    kind: EventKind
    payload: object


@dataclass
class ApprovalProposal:
    """One synchronous permission decision bridged to the Tk thread."""

    tool_name: str
    arguments: dict[str, Any]
    reason: str
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _approved: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def decide(self, approved: bool) -> None:
        with self._lock:
            if self._ready.is_set():
                return
            self._approved = approved is True
            self._ready.set()

    def wait(self) -> bool:
        self._ready.wait()
        return self._approved


class ApprovalBridge:
    """Turn a worker permission callback into a main-thread UI proposal."""

    def __init__(self, events: queue.Queue[UIEvent]) -> None:
        self._events = events
        self._lock = threading.Lock()
        self._closing = False
        self._pending: dict[int, ApprovalProposal] = {}

    def request(self, request: ApprovalRequest) -> bool:
        proposal = ApprovalProposal(
            request.tool_name,
            dict(request.arguments),
            request.reason,
        )
        with self._lock:
            if self._closing:
                return False
            self._pending[id(proposal)] = proposal
        self._events.put(UIEvent("approval", proposal))
        try:
            return proposal.wait()
        finally:
            with self._lock:
                self._pending.pop(id(proposal), None)

    def decide(self, proposal: ApprovalProposal, approved: bool) -> None:
        with self._lock:
            pending = id(proposal) in self._pending
            closing = self._closing
        if pending:
            proposal.decide(approved and not closing)

    def begin_closing(self) -> None:
        """Reject current and future approvals so no worker remains blocked."""
        with self._lock:
            self._closing = True
            pending = tuple(self._pending.values())
        for proposal in pending:
            proposal.decide(False)


class GUIAgent(Protocol):
    model: str
    subagent_model: str
    kind: AgentKind

    @property
    def sensitive_values(self) -> tuple[str, ...]: ...

    def configure_model(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
    ) -> None: ...

    def clear_model(self) -> None: ...

    def configure_subagent_model(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
    ) -> None: ...

    def clear_subagent_model(self) -> None: ...

    def set_kind(self, kind: AgentKind) -> None: ...

    def set_stream_callback(self, callback: StreamCallback | None) -> None: ...

    def snapshot_history(self) -> list[object]: ...

    def restore_history(self, history: list[object]) -> None: ...

    def run(self, prompt: str) -> str: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...


class DeferredAgent:
    """Own one session's lazy runtime, credentials, workspace, and history."""

    def __init__(
        self,
        approval_callback: ApprovalCallback,
        *,
        model: str = "",
        api_key: str = "",
        base_url: str | None = None,
        subagent_model: str = "",
        subagent_api_key: str = "",
        subagent_base_url: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        kind: AgentKind = "main",
        settings: DesktopSettings | None = None,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        if kind not in AGENT_KIND_LABELS:
            raise ValueError("kind must be 'main' or 'sub'")
        self._approval_callback = approval_callback
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._base_url = base_url.strip() if base_url else None
        self._subagent_model = subagent_model.strip()
        self._subagent_api_key = subagent_api_key.strip()
        self._subagent_base_url = subagent_base_url.strip() if subagent_base_url else None
        if bool(self._model) != bool(self._api_key):
            raise ValueError("model and api_key must be configured together")
        if bool(self._subagent_model) != bool(self._subagent_api_key):
            raise ValueError("subagent_model and subagent_api_key must be configured together")
        self.cwd = Path(cwd or Path.cwd()).resolve()
        self._kind = kind
        self._settings = settings or DesktopSettings.defaults()
        self._runtime_factory = runtime_factory or create_runtime
        self._stream_callback: StreamCallback | None = None
        self._agent: AgentLoop | None = None
        self._history: list[object] = []
        self._closed = False
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return (
            f"DeferredAgent(model={self.model!r}, cwd={str(self.cwd)!r}, "
            f"subagent_model={self.subagent_model!r}, kind={self.kind!r}, "
            f"loaded={self.loaded})"
        )

    @property
    def model(self) -> str:
        with self._lock:
            return self._model

    @property
    def kind(self) -> AgentKind:
        with self._lock:
            return self._kind

    @property
    def subagent_model(self) -> str:
        with self._lock:
            return self._subagent_model

    @property
    def loaded(self) -> bool:
        with self._lock:
            return self._agent is not None

    @property
    def sensitive_values(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                dict.fromkeys(
                    value
                    for value in (
                        self._api_key, self._base_url,
                        self._subagent_api_key, self._subagent_base_url,
                    )
                    if value
                )
            )

    def snapshot_history(self) -> list[object]:
        """Return a thread-safe copy of the currently authoritative protocol history."""
        with self._lock:
            source = getattr(self._agent, "history", ()) if self._agent is not None else self._history
            return list(source)

    def restore_history(self, history: list[object]) -> None:
        """Restore plain response input items before the lazy runtime is created."""
        if not isinstance(history, list):
            raise TypeError("history must be a list")
        with self._lock:
            self._assert_open()
            if self._agent is not None:
                raise RuntimeError("cannot restore history after runtime creation")
            self._history = list(history)

    def configure_model(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
    ) -> None:
        selected_model = model.strip()
        selected_key = api_key.strip()
        selected_base_url = base_url.strip() if base_url else None
        if not selected_model or not selected_key:
            raise ValueError("model and api_key must be non-empty")
        old_agent = self._detach_runtime(preserve_history=True)
        with self._lock:
            self._assert_open()
            self._model = selected_model
            self._api_key = selected_key
            self._base_url = selected_base_url
        if old_agent is not None:
            old_agent.close()

    def clear_model(self) -> None:
        old_agent = self._detach_runtime(preserve_history=True)
        with self._lock:
            self._model = ""
            self._api_key = ""
            self._base_url = None
        if old_agent is not None:
            old_agent.reset()
            old_agent.close()

    def configure_subagent_model(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
    ) -> None:
        selected_model = model.strip()
        selected_key = api_key.strip()
        selected_base_url = base_url.strip() if base_url else None
        if not selected_model or not selected_key:
            raise ValueError("model and api_key must be non-empty")
        old_agent = self._detach_runtime(preserve_history=True)
        with self._lock:
            self._assert_open()
            self._subagent_model = selected_model
            self._subagent_api_key = selected_key
            self._subagent_base_url = selected_base_url
        if old_agent is not None:
            old_agent.close()

    def clear_subagent_model(self) -> None:
        old_agent = self._detach_runtime(preserve_history=True)
        with self._lock:
            self._subagent_model = ""
            self._subagent_api_key = ""
            self._subagent_base_url = None
        if old_agent is not None:
            old_agent.close()

    def set_kind(self, kind: AgentKind) -> None:
        if kind not in AGENT_KIND_LABELS:
            raise ValueError("kind must be 'main' or 'sub'")
        old_agent = self._detach_runtime(preserve_history=True)
        with self._lock:
            self._assert_open()
            self._kind = kind
        if old_agent is not None:
            old_agent.close()

    def configure_settings(self, settings: DesktopSettings) -> None:
        old_agent = self.swap_settings(settings)
        if old_agent is not None:
            old_agent.close()

    def swap_settings(self, settings: DesktopSettings) -> AgentLoop | None:
        """Commit settings without invoking fallible runtime cleanup."""
        if not isinstance(settings, DesktopSettings):
            raise TypeError("settings must be DesktopSettings")
        with self._lock:
            self._assert_open()
            old_agent, self._agent = self._agent, None
            if old_agent is not None:
                self._history = list(getattr(old_agent, "history", ()))
            self._settings = settings
            return old_agent

    def set_stream_callback(self, callback: StreamCallback | None) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("stream callback must be callable")
        with self._lock:
            self._stream_callback = callback
            if self._agent is not None:
                self._agent.stream_callback = callback

    def run(self, prompt: str) -> str:
        try:
            return self._get_or_create().run(prompt)
        except SystemExit as exc:
            # CLI configuration validation uses SystemExit; a desktop worker
            # must turn it into a normal result event instead of dying silently.
            raise RuntimeError(str(exc)) from None

    def reset(self) -> None:
        old_agent = self._detach_runtime(preserve_history=False)
        if old_agent is not None:
            old_agent.reset()
            old_agent.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            agent, self._agent = self._agent, None
            self._history.clear()
            self._api_key = ""
            self._base_url = None
            self._subagent_api_key = ""
            self._subagent_base_url = None
        if agent is not None:
            agent.close()

    def _detach_runtime(self, *, preserve_history: bool) -> AgentLoop | None:
        with self._lock:
            agent, self._agent = self._agent, None
            if preserve_history and agent is not None:
                self._history = list(getattr(agent, "history", ()))
            elif not preserve_history:
                self._history.clear()
            return agent

    def _get_or_create(self) -> AgentLoop:
        with self._lock:
            self._assert_open()
            if not self._model or not self._api_key:
                raise RuntimeError("请先注册并选择模型")
            if self._agent is None:
                agent = self._runtime_factory(
                    self._approval_callback,
                    model=self._model,
                    api_key=self._api_key,
                    base_url=self._base_url,
                    subagent_model=self._subagent_model,
                    subagent_api_key=self._subagent_api_key,
                    subagent_base_url=self._subagent_base_url,
                    cwd=self.cwd,
                    kind=self._kind,
                    settings=self._settings,
                    stream_callback=self._stream_callback,
                )
                if self._history and hasattr(agent, "history"):
                    agent.history = list(self._history)
                self._agent = agent
            return self._agent

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("conversation runtime is closed")


class _StreamingRedactor:
    """Redact secrets even when their characters span multiple stream deltas."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self._secrets = tuple(
            sorted({secret for secret in secrets if secret}, key=len, reverse=True)
        )
        self._pending = ""

    def feed(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("stream delta must be a string")
        self._pending += text
        output: list[str] = []
        while self._pending:
            matches = [
                secret for secret in self._secrets if self._pending.startswith(secret)
            ]
            if matches:
                match = matches[0]
                if len(self._pending) == len(match) and any(
                    secret.startswith(self._pending) and len(secret) > len(self._pending)
                    for secret in self._secrets
                ):
                    break
                output.append("[redacted]")
                self._pending = self._pending[len(match) :]
                continue
            if any(secret.startswith(self._pending) for secret in self._secrets):
                break
            output.append(self._pending[0])
            self._pending = self._pending[1:]
        return "".join(output)


class ConversationController:
    """Own one session's idle/busy/closing state and worker thread."""

    def __init__(
        self,
        agent: GUIAgent,
        *,
        events: queue.Queue[UIEvent] | None = None,
        approvals: ApprovalBridge | None = None,
    ) -> None:
        self.agent = agent
        self.events: queue.Queue[UIEvent] = events or queue.Queue()
        self.approvals = approvals or ApprovalBridge(self.events)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="myagent-gui",
        )
        self._lock = threading.Lock()
        self._state: Literal["idle", "busy", "closing", "closed"] = "idle"
        self._running = False
        self._agent_closed = False
        self._has_started = False
        self.last_error = ""
        self._stream_redactor = _StreamingRedactor(())
        self.agent.set_stream_callback(self._on_stream)

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def model(self) -> str:
        return self.agent.model

    @property
    def kind(self) -> AgentKind:
        return self.agent.kind

    @property
    def subagent_model(self) -> str:
        return self.agent.subagent_model

    @property
    def has_started(self) -> bool:
        with self._lock:
            return self._has_started

    def restore_started(self, started: bool) -> None:
        """Restore the kind-lock bit without creating a worker or runtime."""
        with self._lock:
            if self._state != "idle":
                raise RuntimeError("can only restore an idle conversation")
            self._has_started = bool(started)

    def set_model(self, record: ModelRecord) -> bool:
        return self.set_agent_model("main", record)

    def set_agent_model(
        self,
        role: AgentKind,
        record: ModelRecord | None,
    ) -> bool:
        with self._lock:
            if self._state != "idle":
                self.last_error = "请求进行中，暂不能修改 Agent 配置"
                return False
            if role == "main":
                if record is None:
                    self.last_error = "主 Agent 必须选择模型"
                    return False
                self.agent.configure_model(record.name, record.api_key, record.base_url)
            elif role == "sub":
                if record is None:
                    self.agent.clear_subagent_model()
                else:
                    self.agent.configure_subagent_model(
                        record.name,
                        record.api_key,
                        record.base_url,
                    )
            else:
                self.last_error = "Agent 配置类型无效"
                return False
            self.last_error = ""
        return True

    def clear_deleted_model(self, name: str) -> bool:
        with self._lock:
            if self._state != "idle":
                return False
            clears_main = self.agent.model == name
            clears_subagent = self.agent.subagent_model == name
            if not clears_main and not clears_subagent:
                return False
            if clears_main:
                self.agent.clear_model()
            if clears_subagent:
                self.agent.clear_subagent_model()
            self.last_error = "所选模型已删除，请重新选择模型"
        return True

    def set_kind(self, kind: AgentKind) -> bool:
        with self._lock:
            if self._state != "idle":
                self.last_error = "请求进行中，暂不能切换 Agent 类型"
                return False
            if self._has_started:
                self.last_error = "Agent 类型已锁定；如需更换请新建对话"
                return False
            self.agent.set_kind(kind)
            self.last_error = ""
        return True

    def configure_settings(self, settings: DesktopSettings) -> bool:
        with self._lock:
            if self._state != "idle":
                self.last_error = "请求进行中，暂不能修改运行设置"
                return False
            self.agent.configure_settings(settings)
            self.last_error = ""
        return True

    def swap_settings(self, settings: DesktopSettings) -> AgentLoop | None:
        with self._lock:
            if self._state != "idle":
                raise RuntimeError("请求进行中，暂不能修改运行设置")
            return self.agent.swap_settings(settings)

    def submit(self, user_input: str) -> bool:
        prompt = user_input.strip()
        if not prompt:
            self.last_error = "请输入消息"
            return False
        with self._lock:
            if self._state != "idle":
                self.last_error = "请求仍在处理中"
                return False
            if not self.agent.model:
                self.last_error = "请先在模型页注册并选择模型"
                return False
            self._state = "busy"
            self._running = True
            self._has_started = True
            self.last_error = ""
        try:
            self._executor.submit(self._run_turn, prompt)
        except BaseException:
            with self._lock:
                self._state = "idle"
                self._running = False
            raise
        return True

    def _run_turn(self, prompt: str) -> None:
        try:
            answer = self.agent.run(prompt)
        except AgentLoopLimitError as exc:
            detail = self.redact_for_ui(str(exc))
            event = UIEvent("error", f"代理执行超过轮次限制：{detail}")
        except Exception as exc:
            detail = self.redact_for_ui(str(exc) or type(exc).__name__)
            event = UIEvent("error", f"请求失败：{detail}")
        else:
            event = UIEvent("assistant", self.redact_for_ui(answer))
        finally:
            with self._lock:
                self._running = False
        self.events.put(event)

    def _on_stream(self, kind: str, text: str) -> None:
        if kind == "start":
            self._stream_redactor = _StreamingRedactor(self.agent.sensitive_values)
            self.events.put(UIEvent("stream_start", ""))
            return
        if kind != "delta":
            raise ValueError(f"unknown stream event: {kind}")
        safe = self._stream_redactor.feed(text)
        if safe:
            self.events.put(UIEvent("stream_delta", safe))

    def consume_result(self) -> bool:
        """Keep busy until the main thread consumes the queued result."""
        with self._lock:
            if self._running:
                raise RuntimeError("cannot consume a result before the worker ends")
            if self._state == "busy":
                self._state = "idle"
                return False
            return self._state == "closing"

    def reset(self) -> bool:
        with self._lock:
            if self._state != "idle":
                return False
            self.agent.reset()
            self._has_started = False
            self.last_error = ""
        return True

    def request_close(self) -> bool:
        with self._lock:
            if self._state == "closed":
                return True
            idle = not self._running
            self._state = "closing"
        self.approvals.begin_closing()
        return idle

    def close_agent_once(self) -> None:
        with self._lock:
            if self._agent_closed:
                return
            if self._running:
                raise RuntimeError("cannot close the agent while a request is running")
            self._agent_closed = True
            self._state = "closed"
        try:
            self.agent.close()
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def redact_for_ui(self, value: object) -> str:
        """Return a display-only copy with this session's secrets removed."""
        safe = str(value)
        for secret in self.agent.sensitive_values:
            if secret:
                safe = safe.replace(secret, "[redacted]")
        return safe


@dataclass
class ConversationSession:
    session_id: int
    title: str
    workspace: Path
    controller: ConversationController
    messages: list[tuple[str, str]] = field(default_factory=list)
    draft: str = ""
    status: str = "就绪"
    streaming: bool = False
    streaming_text: str = ""
    _stable_messages: list[tuple[str, str]] = field(default_factory=list, repr=False)
    _stable_history: list[object] = field(default_factory=list, repr=False)


class SessionManager:
    """Headless ownership model for tabs, models, and per-session runtimes."""

    def __init__(
        self,
        *,
        store: ModelStore | None = None,
        settings_store: DesktopSettingsStore | None = None,
        conversation_store: ConversationStore | None = None,
        default_cwd: str | os.PathLike[str] | None = None,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        self.store = store or ModelStore()
        self.settings_store = settings_store or DesktopSettingsStore(
            self.store.path.with_name("settings.json")
        )
        self.conversation_store = conversation_store or ConversationStore(
            self.store.path.with_name("conversations.json")
        )
        self.default_cwd = Path(default_cwd or Path.cwd()).resolve()
        self.runtime_factory = runtime_factory
        self.sessions: list[ConversationSession] = []
        self.active_session_id = 0
        self.last_error = ""
        self.model_error = ""
        self.settings_error = ""
        self.conversation_error = ""
        self._next_id = 1
        self._models: list[ModelRecord] = []
        try:
            self.settings = self.settings_store.load()
        except DesktopSettingsError as exc:
            self.settings = DesktopSettings.defaults()
            self.settings_error = str(exc)
        self.refresh_models()
        try:
            saved = self.conversation_store.load()
        except ConversationStoreError as exc:
            self.conversation_error = str(exc)
            self.last_error = self.conversation_error
            self._create_session(self.default_cwd)
        else:
            if saved is None:
                self._create_session(self.default_cwd)
                self._persist()
            else:
                self._restore_conversations(saved)

    @property
    def active(self) -> ConversationSession:
        for session in self.sessions:
            if session.session_id == self.active_session_id:
                return session
        raise RuntimeError("active conversation is unavailable")

    @property
    def models(self) -> tuple[ModelRecord, ...]:
        return tuple(self._models)

    @property
    def model_names(self) -> tuple[str, ...]:
        return tuple(record.name for record in self._models)

    def refresh_models(self) -> bool:
        try:
            self._models = self.store.load()
        except ModelStoreError as exc:
            self._models = []
            self.model_error = str(exc)
            self.last_error = self.model_error
            return False
        self.model_error = ""
        self.last_error = ""
        return True

    def new_session(
        self,
        cwd: str | os.PathLike[str] | None = None,
    ) -> ConversationSession:
        workspace = Path(cwd or self.default_cwd).resolve()
        previous_active = self.active_session_id
        session = self._create_session(workspace)
        if not self._persist():
            self.sessions.remove(session)
            self.active_session_id = previous_active
            try:
                session.controller.request_close()
                session.controller.close_agent_once()
            except Exception:
                pass
            raise ConversationStoreError(self.conversation_error)
        self.last_error = ""
        return session

    def _create_session(
        self,
        workspace: Path,
        *,
        session_id: int | None = None,
        title: str | None = None,
        main_model: str = "",
        sub_model: str = "",
        kind: AgentKind = "main",
        messages: list[tuple[str, str]] | None = None,
        history: list[object] | None = None,
    ) -> ConversationSession:
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise ValueError(f"工作目录无效：{workspace}")
        events: queue.Queue[UIEvent] = queue.Queue()
        approvals = ApprovalBridge(events)
        main_record = next((item for item in self._models if item.name == main_model), None)
        sub_record = next((item for item in self._models if item.name == sub_model), None)
        agent = DeferredAgent(
            approvals.request,
            model=main_record.name if main_record else "",
            api_key=main_record.api_key if main_record else "",
            base_url=main_record.base_url if main_record else None,
            subagent_model=sub_record.name if sub_record else "",
            subagent_api_key=sub_record.api_key if sub_record else "",
            subagent_base_url=sub_record.base_url if sub_record else None,
            cwd=workspace,
            kind=kind,
            settings=self.settings,
            runtime_factory=self.runtime_factory,
        )
        restored_history = list(history or [])
        agent.restore_history(restored_history)
        controller = ConversationController(
            agent,
            events=events,
            approvals=approvals,
        )
        restored_messages = list(messages or [])
        controller.restore_started(bool(restored_history or restored_messages))
        selected_id = self._next_id if session_id is None else session_id
        self._next_id = max(self._next_id, selected_id + 1)
        selected_title = title or f"对话 {selected_id} · {workspace.name or workspace.drive}"
        session = ConversationSession(
            selected_id, selected_title, workspace, controller, restored_messages
        )
        session._stable_messages = list(restored_messages)
        session._stable_history = list(restored_history)
        missing = [name for name in (main_model, sub_model) if name and name not in self.model_names]
        if missing:
            session.status = "模型已缺失，请重新选择；历史已保留"
        self.sessions.append(session)
        self.active_session_id = selected_id
        return session

    def _restore_conversations(self, saved: dict[str, Any]) -> None:
        issues: list[str] = []
        for item in saved["sessions"]:
            workspace = Path(item["workspace"])
            if not workspace.is_dir():
                issues.append(f"已跳过不存在的工作目录：{workspace}")
                continue
            messages = [(entry["speaker"], entry["text"]) for entry in item["messages"]]
            self._create_session(
                workspace,
                session_id=item["id"],
                title=item["title"],
                main_model=item["mainModel"],
                sub_model=item["subModel"],
                kind=item["kind"],
                messages=messages,
                history=list(item["history"]),
            )
            missing = [
                name for name in (item["mainModel"], item["subModel"])
                if name and name not in self.model_names
            ]
            if missing:
                issues.append(f"对话 {item['id']} 的模型已缺失：{', '.join(missing)}")
        self._next_id = max(saved["nextSessionId"], self._next_id)
        if not self.sessions:
            self._create_session(self.default_cwd)
            issues.append("没有可恢复的工作目录，已创建安全空白对话")
        restored_ids = {session.session_id for session in self.sessions}
        self.active_session_id = (
            saved["activeSessionId"]
            if saved["activeSessionId"] in restored_ids
            else self.sessions[0].session_id
        )
        if issues:
            self.conversation_error = "；".join(issues)
            self.last_error = self.conversation_error

    def update_settings(self, values: object) -> bool:
        try:
            proposed = self.settings_store.validate(values)
        except DesktopSettingsError as exc:
            self.settings_error = str(exc)
            self.last_error = self.settings_error
            return False
        if any(session.controller.state != "idle" for session in self.sessions):
            self.settings_error = "有对话正在运行或关闭，暂不能保存运行设置"
            self.last_error = self.settings_error
            return False
        try:
            self.settings_store.save(proposed)
        except DesktopSettingsError as exc:
            self.settings_error = str(exc)
            self.last_error = self.settings_error
            return False
        retired: list[AgentLoop] = []
        for session in self.sessions:
            old_agent = session.controller.swap_settings(proposed)
            if old_agent is not None:
                retired.append(old_agent)
        self.settings = proposed
        self.settings_error = ""
        self.last_error = ""
        # Cleanup is deliberately outside the committed state transition. A
        # close failure cannot split persisted, manager, or per-session values.
        for old_agent in retired:
            try:
                old_agent.close()
            except Exception:
                pass
        return True

    def open_folder(self, selected: str | os.PathLike[str] | None) -> ConversationSession | None:
        if selected is None or not str(selected).strip():
            return None
        return self.new_session(selected)

    def activate(self, session_id: int) -> bool:
        if any(session.session_id == session_id for session in self.sessions):
            previous = self.active_session_id
            self.active_session_id = session_id
            if not self._persist():
                self.active_session_id = previous
                return False
            self.last_error = ""
            return True
        return False

    def rename_session(self, session_id: int, title: str) -> bool:
        session = next(
            (item for item in self.sessions if item.session_id == session_id),
            None,
        )
        if session is None:
            self.last_error = "对话不存在"
            return False
        normalized = " ".join(title.split())
        if not normalized:
            self.last_error = "会话名称不能为空"
            return False
        if len(normalized) > 80:
            self.last_error = "会话名称不能超过 80 个字符"
            return False
        previous = session.title
        session.title = normalized
        if not self._persist():
            session.title = previous
            return False
        self.last_error = ""
        return True

    def close_session(self, session_id: int) -> bool:
        """Permanently delete an idle conversation after the disk proposal commits."""
        session = next(
            (item for item in self.sessions if item.session_id == session_id),
            None,
        )
        if session is None:
            return False
        if len(self.sessions) == 1:
            self.last_error = "至少保留一个对话标签"
            return False
        if session.controller.state != "idle":
            self.last_error = "该对话正在运行，完成后才能关闭"
            return False
        index = self.sessions.index(session)
        remaining = [item for item in self.sessions if item is not session]
        next_active = self.active_session_id
        if next_active == session_id:
            next_active = remaining[min(index, len(remaining) - 1)].session_id
        if not self._persist(exclude_session_id=session_id, active_id=next_active):
            return False
        self.sessions.remove(session)
        self.active_session_id = next_active
        try:
            session.controller.request_close()
            session.controller.close_agent_once()
        except Exception:
            # The disk and owner transition already committed. Cleanup is
            # intentionally best-effort and must never resurrect the record.
            pass
        self.last_error = ""
        return True

    delete_session = close_session

    def select_model(self, session_id: int, name: str) -> bool:
        return self.configure_agent_model(session_id, "main", name)

    def configure_agent_model(
        self,
        session_id: int,
        role: AgentKind,
        name: str | None,
    ) -> bool:
        record = next((item for item in self._models if item.name == name), None)
        session = next(
            (item for item in self.sessions if item.session_id == session_id),
            None,
        )
        if session is None:
            self.last_error = "对话不存在"
            return False
        if role not in {"main", "sub"}:
            self.last_error = "Agent 配置类型无效"
            return False
        if role == "main" and record is None:
            self.last_error = "模型不存在，请刷新后重试"
            return False
        if role == "sub" and name is not None and record is None:
            self.last_error = "模型不存在，请刷新后重试"
            return False
        if session.controller.state != "idle":
            self.last_error = "请求进行中，暂不能修改 Agent 配置"
            return False
        proposed_name = record.name if record is not None else ""
        if not self._persist(model_overrides={(session_id, role): proposed_name}):
            return False
        selected = session.controller.set_agent_model(role, record)
        self.last_error = session.controller.last_error
        return selected

    def set_kind(self, session_id: int, kind: AgentKind) -> bool:
        session = next(
            (item for item in self.sessions if item.session_id == session_id), None
        )
        if session is None:
            self.last_error = "对话不存在"
            return False
        previous = session.controller.kind
        if not session.controller.set_kind(kind):
            self.last_error = session.controller.last_error
            return False
        if not self._persist():
            # This rollback is safe because kind changes are allowed only before a turn.
            session.controller.set_kind(previous)
            return False
        self.last_error = ""
        return True

    def register_model(
        self,
        name: str,
        api_key: str,
        base_url: str | None = None,
    ) -> ModelRecord:
        record = self.store.add(name, api_key, base_url)
        self._models = self.store.load()
        self.last_error = ""
        return record

    def delete_model(self, name: str) -> bool:
        if any(
            (
                session.controller.model == name
                or session.controller.subagent_model == name
            )
            and session.controller.state in {"busy", "closing"}
            for session in self.sessions
        ):
            self.last_error = "模型正在被运行中的对话使用，暂不能删除"
            return False
        original_models = list(self._models)
        if not any(record.name == name for record in original_models):
            self.last_error = "模型不存在"
            return False
        overrides: dict[tuple[int, AgentKind], str] = {}
        for session in self.sessions:
            if session.controller.model == name:
                overrides[(session.session_id, "main")] = ""
            if session.controller.subagent_model == name:
                overrides[(session.session_id, "sub")] = ""
        try:
            deleted = self.store.delete(name)
        except ModelStoreError as exc:
            self.last_error = str(exc)
            return False
        if not deleted:
            self.last_error = "模型不存在"
            return False
        if not self._persist(model_overrides=overrides):
            try:
                self.store._write(original_models)
            except ModelStoreError as exc:
                self.last_error = f"{self.conversation_error}；模型配置回滚失败：{exc}"
            return False
        for session in self.sessions:
            if session.controller.clear_deleted_model(name):
                session.status = "所选模型已删除，请重新选择"
        self._models = self.store.load()
        self.last_error = ""
        return True

    def persist_completed_turn(self, session_id: int) -> bool:
        session = next(
            (item for item in self.sessions if item.session_id == session_id), None
        )
        if session is None:
            self.last_error = "对话不存在"
            return False
        if session.controller.state not in {"idle", "closing"}:
            self.last_error = "对话尚未完成，未保存临时历史"
            return False
        return self._persist(force_completed_session_id=session_id)

    def _payload(
        self,
        *,
        exclude_session_id: int | None = None,
        active_id: int | None = None,
        model_overrides: dict[tuple[int, AgentKind], str] | None = None,
        force_completed_session_id: int | None = None,
    ) -> dict[str, Any]:
        sessions: list[dict[str, Any]] = []
        for session in self.sessions:
            if session.session_id == exclude_session_id:
                continue
            controller = session.controller
            stable_only = controller.state != "idle" and session.session_id != force_completed_session_id
            messages = session._stable_messages if stable_only else session.messages
            history = (
                session._stable_history
                if stable_only
                else controller.agent.snapshot_history()
            )
            sessions.append({
                "id": session.session_id,
                "title": session.title,
                "workspace": str(session.workspace),
                "mainModel": (model_overrides or {}).get((session.session_id, "main"), controller.model),
                "subModel": (model_overrides or {}).get((session.session_id, "sub"), controller.subagent_model),
                "kind": controller.kind,
                "messages": [
                    {"speaker": speaker, "text": text}
                    for speaker, text in messages
                ],
                "history": history,
            })
        selected_active = active_id if active_id is not None else self.active_session_id
        return {
            "version": SCHEMA_VERSION,
            "activeSessionId": selected_active,
            "nextSessionId": self._next_id,
            "sessions": sessions,
        }

    def _persist(
        self,
        *,
        exclude_session_id: int | None = None,
        active_id: int | None = None,
        model_overrides: dict[tuple[int, AgentKind], str] | None = None,
        force_completed_session_id: int | None = None,
    ) -> bool:
        payload = self._payload(
            exclude_session_id=exclude_session_id,
            active_id=active_id,
            model_overrides=model_overrides,
            force_completed_session_id=force_completed_session_id,
        )
        secrets = tuple(
            secret
            for values in (
                *(session.controller.agent.sensitive_values for session in self.sessions),
                *((record.api_key, record.base_url or "") for record in self._models),
            )
            for secret in values
        )
        previous_error = self.conversation_error
        try:
            self.conversation_store.save(payload, sensitive_values=secrets)
        except ConversationStoreError as exc:
            self.conversation_error = str(exc)
            self.last_error = self.conversation_error
            return False
        self.conversation_error = ""
        if previous_error and self.last_error == previous_error:
            self.last_error = ""
        for session in self.sessions:
            if (
                session.session_id != exclude_session_id
                and (
                    session.controller.state == "idle"
                    or session.session_id == force_completed_session_id
                )
            ):
                session._stable_messages = list(session.messages)
                session._stable_history = session.controller.agent.snapshot_history()
        return True

    def begin_close_all(self) -> bool:
        all_idle = True
        for session in self.sessions:
            if session.controller.state == "closed":
                continue
            if session.controller.request_close():
                session.controller.close_agent_once()
            else:
                all_idle = False
        return all_idle

    def all_closed(self) -> bool:
        return all(session.controller.state == "closed" for session in self.sessions)


def create_runtime(
    approval_callback: ApprovalCallback,
    *,
    model: str,
    api_key: str,
    base_url: str | None = None,
    subagent_model: str = "",
    subagent_api_key: str = "",
    subagent_base_url: str | None = None,
    cwd: str | os.PathLike[str],
    kind: AgentKind,
    settings: DesktopSettings | None = None,
    stream_callback: StreamCallback | None = None,
) -> AgentLoop:
    """Create a runtime using only this session's registered credentials."""
    file_config = _load_config()
    defaults = AgentConfig()
    fallback_model = file_config.get("fallback_model", defaults.fallback_model)
    config = _config_from_environment(model, fallback_model, file_config)
    selected_settings = settings or DesktopSettings.defaults()
    if settings is not None:
        config = replace(
            config,
            max_tool_rounds=selected_settings.max_tool_rounds,
            bash_timeout_seconds=selected_settings.bash_timeout_seconds,
            todo_reminder_tool_calls=selected_settings.todo_reminder_tool_calls,
            subagent_max_workers=selected_settings.subagent_max_workers,
            subagent_max_tasks=selected_settings.subagent_max_tasks,
        )

    from openai import OpenAI

    options = _client_options(file_config)
    options["api_key"] = api_key
    if base_url:
        options["base_url"] = base_url
    client = OpenAI(**options)
    factory = create_default_agent if kind == "main" else create_default_subagent
    if kind == "sub" or not subagent_model:
        return factory(
            client,
            config=config,
            cwd=Path(cwd),
            approval_callback=approval_callback,
            stream_callback=stream_callback,
        )

    subagent_config = _config_from_environment(
        subagent_model,
        fallback_model,
        file_config,
    )
    if settings is not None:
        subagent_config = replace(
            subagent_config,
            max_tool_rounds=selected_settings.max_tool_rounds,
            bash_timeout_seconds=selected_settings.bash_timeout_seconds,
            todo_reminder_tool_calls=selected_settings.todo_reminder_tool_calls,
            subagent_max_workers=selected_settings.subagent_max_workers,
            subagent_max_tasks=selected_settings.subagent_max_tasks,
        )
    subagent_options = _client_options(file_config)
    subagent_options["api_key"] = subagent_api_key
    if subagent_base_url:
        subagent_options["base_url"] = subagent_base_url
    subagent_client = OpenAI(**subagent_options)
    return factory(
        client,
        config=config,
        subagent_client=subagent_client,
        subagent_model=subagent_config.model,
        subagent_fallback_model=subagent_config.fallback_model,
        cwd=Path(cwd),
        approval_callback=approval_callback,
        stream_callback=stream_callback,
    )


def initial_model() -> str:
    """Compatibility helper; GUI selection itself comes only from ModelStore."""
    defaults = AgentConfig()
    try:
        file_config = _load_config()
    except SystemExit:
        file_config = {}
    selected = os.getenv("OPENAI_MODEL", file_config.get("model", defaults.model))
    return selected.strip() or defaults.model


class MyAgentWindow:
    """OpenCode-inspired widgets; all Tk access stays on the main thread."""

    _POLL_MS = 50

    def __init__(
        self,
        root: tk.Tk | ctk.CTk,
        controller: ConversationController | None = None,
        *,
        manager: SessionManager | None = None,
        store: ModelStore | None = None,
        default_cwd: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = root
        self.manager = manager or SessionManager(store=store, default_cwd=default_cwd)
        if controller is not None:
            old = self.manager.sessions[0]
            old.controller.request_close()
            old.controller.close_agent_once()
            old.controller = controller
        self._destroyed = False
        self._closing_all = False
        self._page: Literal["conversation", "models"] = "conversation"
        self._tab_widgets: list[tk.Widget] = []
        self._model_row_widgets: list[tk.Widget] = []

        root.title("MyAgent 工作台")
        if isinstance(root, ctk.CTk):
            root.configure(fg_color=_APP_BACKGROUND)
        else:
            root.configure(background=_APP_BACKGROUND)
        root.geometry("960x640")
        root.minsize(720, 520)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        self._configure_app_icon()
        self._configure_theme()
        self._build_header()
        self._build_tabs()
        self._build_pages()
        self.show_conversation()
        self._rebuild_tabs()
        self._refresh_model_choices()
        self._render_active()
        if self.manager.model_error:
            self.status.set(self.manager.model_error)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._poll_after_id = root.after(self._POLL_MS, self._poll_events)
        self._compact_layout: bool | None = None
        root.bind("<Configure>", self._on_window_resize, add="+")

    def _configure_app_icon(self) -> None:
        """Install a small, dependency-free MyAgent mark after CTk's default."""
        icon = tk.PhotoImage(master=self.root, width=32, height=32)
        for y, left, right in (
            (2, 7, 25),
            (3, 5, 27),
            (4, 4, 28),
            *[(row, 3, 29) for row in range(5, 27)],
            (27, 4, 28),
            (28, 5, 27),
            (29, 7, 25),
        ):
            icon.put(_ACCENT_COLOR, to=(left, y, right, y + 1))
        segments = (
            (9, 24, 9, 9),
            (9, 9, 16, 18),
            (16, 18, 23, 9),
            (23, 9, 23, 24),
            (18, 16, 23, 20),
        )
        for y in range(32):
            for x in range(32):
                for x1, y1, x2, y2 in segments:
                    dx = x2 - x1
                    dy = y2 - y1
                    length_squared = dx * dx + dy * dy
                    offset = ((x - x1) * dx + (y - y1) * dy) / length_squared
                    position = max(0.0, min(1.0, offset))
                    nearest_x = x1 + position * dx
                    nearest_y = y1 + position * dy
                    if (x - nearest_x) ** 2 + (y - nearest_y) ** 2 <= 2.25:
                        icon.put(_ACCENT_TEXT_COLOR, to=(x, y, x + 1, y + 1))
                        break
        self._app_icon = icon
        if isinstance(self.root, ctk.CTk):
            # Calling iconbitmap marks the icon as user-owned so CTk does not
            # replace it with the library logo a moment after startup.
            self.root.iconbitmap()
        self.root.iconphoto(True, self._app_icon)
        self.root.after(260, lambda: self.root.iconphoto(True, self._app_icon))

    def _configure_theme(self) -> None:
        self.root.option_add("*Font", (_FONT_FAMILY, _FONT_SIZE_CONTROL))
        self.root.option_add("*Menu.Font", (_FONT_FAMILY, _FONT_SIZE_CONTROL))
        self.root.option_add("*Menu.borderWidth", 0)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(
            self.root,
            fg_color=_HEADER_BACKGROUND,
            corner_radius=0,
            height=64,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(5, weight=1)

        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.grid(row=0, column=0, padx=(18, 22), pady=12, sticky="w")
        self.header_logo = tk.Canvas(
            brand,
            width=34,
            height=34,
            background=_HEADER_BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
        )
        self.header_logo.grid(row=0, column=0, rowspan=2, padx=(0, 10))
        self.header_logo.create_rectangle(8, 1, 26, 33, fill=_ACCENT_COLOR, outline="")
        self.header_logo.create_rectangle(1, 8, 33, 26, fill=_ACCENT_COLOR, outline="")
        for bounds in ((1, 1, 15, 15), (19, 1, 33, 15), (1, 19, 15, 33), (19, 19, 33, 33)):
            self.header_logo.create_oval(*bounds, fill=_ACCENT_COLOR, outline="")
        self.header_logo.create_line(
            9, 25, 9, 10, 17, 19, 25, 10, 25, 25,
            fill=_ACCENT_TEXT_COLOR,
            width=4,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )
        self.header_logo.create_line(
            19, 17, 25, 21,
            fill=_ACCENT_TEXT_COLOR,
            width=4,
            capstyle=tk.ROUND,
        )
        ctk.CTkLabel(
            brand,
            text="MyAgent",
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_BRAND, "bold"),
            anchor="w",
            height=18,
        ).grid(row=0, column=1, sticky="sw")
        ctk.CTkLabel(
            brand,
            text="本地 Agent 工作台",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="w",
            height=14,
        ).grid(row=1, column=1, sticky="nw")

        self.file_button = ctk.CTkButton(
            header,
            text="文件  ▾",
            command=self._show_file_menu,
            width=84,
            height=36,
            corner_radius=8,
            fg_color="transparent",
            hover_color=_RAISED_BACKGROUND,
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            anchor="center",
        )
        self.file_button.grid(row=0, column=1, padx=(0, 4), pady=14)
        file_menu = tk.Menu(
            self.root,
            tearoff=False,
            background=_INPUT_BACKGROUND,
            foreground=_TEXT_COLOR,
            activebackground=_RAISED_BACKGROUND,
            activeforeground=_TEXT_COLOR,
            disabledforeground=_MUTED_TEXT_COLOR,
            relief=tk.FLAT,
            borderwidth=1,
        )
        file_menu.add_command(label="新建对话    Ctrl+N", command=self.new_session)
        file_menu.add_command(label="打开文件夹  Ctrl+O", command=self.open_folder)
        self.file_menu = file_menu
        self.models_button = ctk.CTkButton(
            header,
            text="模型",
            command=self.show_models,
            width=72,
            height=36,
            corner_radius=8,
            fg_color="transparent",
            hover_color=_RAISED_BACKGROUND,
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
        )
        self.models_button.grid(row=0, column=2, padx=4, pady=14)
        ctk.CTkButton(
            header,
            text="设置",
            width=72,
            height=36,
            corner_radius=8,
            fg_color="transparent",
            text_color_disabled="#556168",
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            state="disabled",
        ).grid(row=0, column=3, padx=4, pady=14)
        self.header_workspace_value = tk.StringVar()
        self.header_workspace_label = ctk.CTkLabel(
            header,
            textvariable=self.header_workspace_value,
            text_color=_MUTED_TEXT_COLOR,
            font=(_MONO_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="e",
        )
        self.header_workspace_label.grid(
            row=0,
            column=5,
            padx=(12, 18),
            pady=14,
            sticky="e",
        )

        self.root.bind("<Control-n>", lambda _event: self.new_session())
        self.root.bind("<Control-o>", lambda _event: self.open_folder())
        self.root.bind("<Escape>", self._on_escape, add="+")

    def _show_file_menu(self) -> None:
        x = self.file_button.winfo_rootx()
        y = self.file_button.winfo_rooty() + self.file_button.winfo_height() + 4
        try:
            self.file_menu.tk_popup(x, y)
        finally:
            self.file_menu.grab_release()

    def _build_tabs(self) -> None:
        tab_bar = ctk.CTkFrame(
            self.root,
            fg_color=_HEADER_BACKGROUND,
            corner_radius=0,
            height=48,
            border_width=0,
        )
        tab_bar.grid(row=1, column=0, sticky="ew")
        tab_bar.grid_propagate(False)
        self.tabs = ctk.CTkFrame(tab_bar, fg_color="transparent")
        self.tabs.grid(row=0, column=0, padx=14, pady=(7, 6), sticky="w")
        ctk.CTkFrame(
            tab_bar,
            fg_color=_BORDER_COLOR,
            height=1,
            corner_radius=0,
        ).place(relx=0, rely=1, relwidth=1, anchor="sw")

    def _build_pages(self) -> None:
        self.page_host = ctk.CTkFrame(
            self.root,
            fg_color=_APP_BACKGROUND,
            corner_radius=0,
        )
        self.page_host.grid(row=2, column=0, sticky="nsew")
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)

        self.conversation_page = ctk.CTkFrame(
            self.page_host,
            fg_color=_APP_BACKGROUND,
            corner_radius=0,
        )
        self.conversation_page.columnconfigure(0, weight=1)
        self.conversation_page.rowconfigure(1, weight=1)

        context_bar = ctk.CTkFrame(
            self.conversation_page,
            fg_color="transparent",
        )
        context_bar.grid(row=0, column=0, padx=22, pady=(16, 10), sticky="ew")
        context_bar.columnconfigure(1, weight=1)
        ctk.CTkLabel(
            context_bar,
            text="工作区",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.workspace_value = tk.StringVar()
        ctk.CTkLabel(
            context_bar,
            textvariable=self.workspace_value,
            text_color=_TEXT_COLOR,
            font=(_MONO_FONT_FAMILY, _FONT_SIZE_CONTROL),
            anchor="w",
        ).grid(row=0, column=1, padx=(10, 14), sticky="w")
        self.status = tk.StringVar(value="就绪")
        self.status_label = ctk.CTkLabel(
            context_bar,
            textvariable=self.status,
            text_color=_MUTED_TEXT_COLOR,
            fg_color=_PANEL_BACKGROUND,
            corner_radius=8,
            height=28,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="center",
        )
        self.status_label.grid(row=0, column=2, ipadx=8, sticky="e")

        self.transcript_host = ctk.CTkFrame(
            self.conversation_page,
            fg_color="transparent",
            corner_radius=0,
        )
        self.transcript_host.grid(row=1, column=0, padx=22, sticky="nsew")
        self.transcript_host.columnconfigure(0, weight=1)
        self.transcript_host.rowconfigure(0, weight=1)

        self.transcript = ctk.CTkTextbox(
            self.transcript_host,
            wrap=tk.WORD,
            state=tk.DISABLED,
            fg_color=_PANEL_BACKGROUND,
            text_color=_TEXT_COLOR,
            border_width=1,
            border_color=_BORDER_COLOR,
            corner_radius=12,
            scrollbar_button_color="#344047",
            scrollbar_button_hover_color="#46545c",
            font=(_FONT_FAMILY, _FONT_SIZE_BODY),
            padx=12,
            pady=10,
        )
        self.transcript.grid(row=0, column=0, sticky="nsew")
        self.transcript.tag_config(
            "speaker",
            foreground=_MUTED_TEXT_COLOR,
            spacing1=12,
            spacing3=3,
        )
        self.transcript.tag_config(
            "message",
            foreground=_TEXT_COLOR,
            spacing3=14,
            lmargin1=8,
            lmargin2=8,
        )
        self.transcript.tag_config(
            "error",
            foreground=_ERROR_COLOR,
            spacing3=14,
            lmargin1=8,
            lmargin2=8,
        )

        self.welcome_panel = ctk.CTkFrame(
            self.transcript_host,
            fg_color="transparent",
            corner_radius=0,
        )
        self.welcome_panel.grid(row=0, column=0, sticky="nsew")
        self.welcome_panel.columnconfigure(0, weight=1)
        self.welcome_panel.rowconfigure(0, weight=1)
        self.welcome_logo = tk.Canvas(
            self.welcome_panel,
            width=340,
            height=230,
            background=_APP_BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
        )
        self.welcome_logo.grid(row=0, column=0)
        self._draw_welcome_logo()

        self.composer = ctk.CTkFrame(
            self.conversation_page,
            fg_color=_INPUT_BACKGROUND,
            border_width=1,
            border_color=_BORDER_COLOR,
            corner_radius=12,
        )
        self.composer.grid(row=2, column=0, padx=22, pady=(12, 18), sticky="ew")
        self.composer.columnconfigure(0, weight=1)
        self.input = tk.Text(
            self.composer,
            wrap=tk.WORD,
            height=4,
            background=_INPUT_BACKGROUND,
            foreground=_TEXT_COLOR,
            insertbackground=_ACCENT_TEXT_COLOR,
            selectbackground="#254f96",
            selectforeground=_ACCENT_TEXT_COLOR,
            borderwidth=0,
            highlightthickness=0,
            relief=tk.FLAT,
            font=(_FONT_FAMILY, _FONT_SIZE_BODY),
            padx=10,
            pady=8,
            undo=True,
            maxundo=50,
        )
        self.input.grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 0))
        self.input.bind("<Control-Return>", self._on_ctrl_enter)
        self.input.bind("<FocusIn>", self._on_input_focus, add="+")
        self.input.bind("<FocusOut>", self._on_input_blur, add="+")

        self.action_bar = ctk.CTkFrame(self.composer, fg_color="transparent")
        self.action_bar.grid(row=1, column=0, padx=12, pady=(2, 12), sticky="ew")
        self.action_bar.columnconfigure(2, weight=1)

        self.model_control = ctk.CTkFrame(self.action_bar, fg_color="transparent")
        self.model_control.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            self.model_control,
            text="模型",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
        ).grid(row=0, column=0, padx=(0, 7))
        self.model_value = tk.StringVar()
        self.model_selector = ctk.CTkOptionMenu(
            self.model_control,
            variable=self.model_value,
            values=["尚无模型"],
            command=self._on_model_selected,
            width=160,
            height=32,
            corner_radius=8,
            fg_color=_PANEL_BACKGROUND,
            button_color=_RAISED_BACKGROUND,
            button_hover_color="#303a40",
            dropdown_fg_color=_PANEL_BACKGROUND,
            dropdown_hover_color=_RAISED_BACKGROUND,
            text_color=_TEXT_COLOR,
            dropdown_text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            dropdown_font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            anchor="w",
        )
        self.model_selector.grid(row=0, column=1)

        self.kind_control = ctk.CTkFrame(self.action_bar, fg_color="transparent")
        self.kind_control.grid(row=0, column=1, padx=(18, 0), sticky="w")
        ctk.CTkLabel(
            self.kind_control,
            text="Agent",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
        ).grid(row=0, column=0, padx=(0, 7))
        self.kind_value = tk.StringVar(value=MAIN_AGENT_LABEL)
        self.kind_selector = ctk.CTkOptionMenu(
            self.kind_control,
            variable=self.kind_value,
            values=[MAIN_AGENT_LABEL, SUBAGENT_LABEL],
            command=self._on_kind_selected,
            width=210,
            height=32,
            corner_radius=8,
            fg_color=_PANEL_BACKGROUND,
            button_color=_RAISED_BACKGROUND,
            button_hover_color="#303a40",
            dropdown_fg_color=_PANEL_BACKGROUND,
            dropdown_hover_color=_RAISED_BACKGROUND,
            text_color=_TEXT_COLOR,
            dropdown_text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            dropdown_font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            anchor="w",
        )
        self.kind_selector.grid(row=0, column=1)

        self.send_hint = ctk.CTkLabel(
            self.action_bar,
            text="Ctrl + Enter 发送",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
        )
        self.send_hint.grid(row=0, column=2, padx=16, sticky="e")
        self.send_button = ctk.CTkButton(
            self.action_bar,
            text="发送  ↑",
            command=self.send,
            width=92,
            height=36,
            corner_radius=9,
            fg_color=_ACCENT_COLOR,
            hover_color=_ACCENT_HOVER_COLOR,
            text_color=_ACCENT_TEXT_COLOR,
            text_color_disabled="#91a6c8",
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL, "bold"),
        )
        self.send_button.grid(row=0, column=3, sticky="e")

        self.models_page = ctk.CTkFrame(
            self.page_host,
            fg_color=_APP_BACKGROUND,
            corner_radius=0,
        )
        self.models_page.columnconfigure(0, weight=1)
        self.models_page.rowconfigure(2, weight=1)

        model_navigation = ctk.CTkFrame(
            self.models_page,
            fg_color="transparent",
        )
        model_navigation.grid(
            row=0,
            column=0,
            padx=26,
            pady=(18, 0),
            sticky="ew",
        )
        self.model_back_button = ctk.CTkButton(
            model_navigation,
            text="←  返回对话",
            command=self.return_to_conversation,
            width=108,
            height=34,
            corner_radius=8,
            fg_color="transparent",
            hover_color=_RAISED_BACKGROUND,
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="w",
        )
        self.model_back_button.grid(row=0, column=0, sticky="w")

        self.model_header = ctk.CTkFrame(self.models_page, fg_color="transparent")
        self.model_header.grid(row=1, column=0, padx=26, pady=(12, 14), sticky="ew")
        self.model_header.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.model_header,
            text="模型与凭据",
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_PAGE_TITLE, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.model_header_subtitle = ctk.CTkLabel(
            self.model_header,
            text="凭据保存在仓库外；每个会话可独立选择模型。",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            anchor="w",
            wraplength=420,
        )
        self.model_header_subtitle.grid(row=1, column=0, pady=(4, 0), sticky="w")
        self.model_add_button = ctk.CTkButton(
            self.model_header,
            text="＋  添加模型",
            command=self.open_register_dialog,
            width=126,
            height=38,
            corner_radius=9,
            fg_color=_ACCENT_COLOR,
            hover_color=_ACCENT_HOVER_COLOR,
            text_color=_ACCENT_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL, "bold"),
        )
        self.model_add_button.grid(
            row=0,
            column=1,
            rowspan=2,
            padx=(18, 0),
            sticky="e",
        )

        self.model_rows = ctk.CTkScrollableFrame(
            self.models_page,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color="#344047",
            scrollbar_button_hover_color="#46545c",
        )
        self.model_rows.grid(row=2, column=0, padx=(20, 10), pady=(0, 8), sticky="nsew")
        self.model_rows.columnconfigure(0, weight=1)
        self.model_page_status = tk.StringVar()
        ctk.CTkLabel(
            self.models_page,
            textvariable=self.model_page_status,
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="w",
        ).grid(row=3, column=0, padx=26, pady=(0, 18), sticky="w")

    def _draw_welcome_logo(self) -> None:
        """Draw the static MyAgent logo once, without resize-bound redraws."""
        canvas = self.welcome_logo
        canvas.delete("all")
        x1, y1, x2, y2, radius = 110, 22, 230, 142, 26
        canvas.create_rectangle(
            x1 + radius,
            y1,
            x2 - radius,
            y2,
            fill=_ACCENT_COLOR,
            outline="",
        )
        canvas.create_rectangle(
            x1,
            y1 + radius,
            x2,
            y2 - radius,
            fill=_ACCENT_COLOR,
            outline="",
        )
        for left, top, right, bottom in (
            (x1, y1, x1 + radius * 2, y1 + radius * 2),
            (x2 - radius * 2, y1, x2, y1 + radius * 2),
            (x1, y2 - radius * 2, x1 + radius * 2, y2),
            (x2 - radius * 2, y2 - radius * 2, x2, y2),
        ):
            canvas.create_oval(
                left,
                top,
                right,
                bottom,
                fill=_ACCENT_COLOR,
                outline="",
            )
        canvas.create_line(
            136,
            116,
            136,
            64,
            170,
            101,
            204,
            64,
            204,
            116,
            fill=_ACCENT_TEXT_COLOR,
            width=13,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
        )
        canvas.create_line(
            178,
            92,
            204,
            107,
            fill=_ACCENT_TEXT_COLOR,
            width=13,
            capstyle=tk.ROUND,
        )
        canvas.create_text(
            170,
            183,
            text="MyAgent",
            fill=_TEXT_COLOR,
            font=(_FONT_FAMILY, 24, "bold"),
        )

    def _on_input_focus(self, _event: tk.Event[tk.Misc]) -> None:
        self.composer.configure(border_color=_SIGNAL_MUTED_COLOR)

    def _on_input_blur(self, _event: tk.Event[tk.Misc]) -> None:
        self.composer.configure(border_color=_BORDER_COLOR)

    def _on_window_resize(self, event: tk.Event[tk.Misc]) -> None:
        """Toggle auxiliary detail only; primary actions keep their grid slots."""
        if event.widget is not self.root or self._destroyed:
            return
        scaling = ctk.ScalingTracker.get_window_scaling(self.root)
        logical_width = event.width / scaling
        if self._compact_layout is None:
            compact = logical_width < _COMPACT_INITIAL_WIDTH
        elif self._compact_layout:
            compact = logical_width < _COMPACT_EXIT_WIDTH
        else:
            compact = logical_width < _COMPACT_ENTER_WIDTH
        if compact == self._compact_layout:
            return
        self._compact_layout = compact
        if compact:
            self.header_workspace_label.grid_remove()
            self.send_hint.grid_remove()
            self.model_header_subtitle.configure(wraplength=340)
        else:
            self.header_workspace_label.grid()
            self.send_hint.grid()
            self.model_header_subtitle.configure(wraplength=420)

    def _rebuild_tabs(self) -> None:
        for widget in self._tab_widgets:
            widget.destroy()
        self._tab_widgets.clear()
        for column, session in enumerate(self.manager.sessions):
            active = session.session_id == self.manager.active_session_id
            frame = ctk.CTkFrame(
                self.tabs,
                fg_color=_ACTIVE_BACKGROUND if active else "transparent",
                border_width=1 if active else 0,
                border_color="#315f9f" if active else _HEADER_BACKGROUND,
                corner_radius=9,
            )
            frame.grid(row=0, column=column, padx=(0, 5))
            button = ctk.CTkButton(
                frame,
                text=session.title,
                command=lambda sid=session.session_id: self.activate_session(sid),
                width=0,
                height=32,
                corner_radius=8,
                fg_color="transparent",
                hover_color=_RAISED_BACKGROUND,
                text_color=_TEXT_COLOR if active else _MUTED_TEXT_COLOR,
                font=(
                    _FONT_FAMILY,
                    _FONT_SIZE_CAPTION,
                    "bold" if active else "normal",
                ),
                anchor="w",
            )
            button.grid(row=0, column=0, padx=(3, 0), pady=2)
            close = ctk.CTkButton(
                frame,
                text="×",
                command=lambda sid=session.session_id: self.close_session(sid),
                width=28,
                height=28,
                corner_radius=7,
                fg_color="transparent",
                hover_color=_DANGER_HOVER_COLOR,
                text_color=_MUTED_TEXT_COLOR,
                font=(_FONT_FAMILY, 13),
            )
            close.grid(row=0, column=1, padx=(0, 3), pady=2)
            self._tab_widgets.extend((frame, button, close))

    def _save_draft(self) -> None:
        if self._page == "conversation" and not self._destroyed:
            self.manager.active.draft = self.input.get("1.0", "end-1c")

    def activate_session(self, session_id: int) -> None:
        self._save_draft()
        if self.manager.activate(session_id):
            self.show_conversation()
            self._rebuild_tabs()
            self._render_active()
        elif self.manager.last_error:
            self.status.set(self.manager.last_error)

    def new_session(self) -> None:
        self._save_draft()
        try:
            self.manager.new_session()
        except ConversationStoreError as exc:
            self.status.set(str(exc))
            return
        self.show_conversation()
        self._rebuild_tabs()
        self._render_active()

    def open_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.root, mustexist=True)
        if not selected:
            return
        try:
            self._save_draft()
            self.manager.open_folder(selected)
        except (ValueError, ConversationStoreError) as exc:
            self.status.set(str(exc))
            return
        self.show_conversation()
        self._rebuild_tabs()
        self._render_active()

    def close_session(self, session_id: int) -> None:
        self._save_draft()
        if not self.manager.close_session(session_id):
            self.status.set(self.manager.last_error)
            return
        self._rebuild_tabs()
        self._render_active()

    def show_conversation(self) -> None:
        self._page = "conversation"
        self.models_page.grid_remove()
        self.conversation_page.grid(row=0, column=0, sticky="nsew")
        self.models_button.configure(fg_color="transparent", text_color=_TEXT_COLOR)

    def return_to_conversation(self) -> None:
        """Leave a secondary page through an explicit, discoverable route."""
        self.show_conversation()
        self._render_active()
        if self.input.cget("state") == "normal":
            self.input.focus_set()

    def _on_escape(self, _event: tk.Event[tk.Misc]) -> str | None:
        if self._page != "models":
            return None
        self.return_to_conversation()
        return "break"

    def show_models(self) -> None:
        self._save_draft()
        self._page = "models"
        self.conversation_page.grid_remove()
        self.models_page.grid(row=0, column=0, sticky="nsew")
        self.models_button.configure(
            fg_color=_ACTIVE_BACKGROUND,
            text_color=_ACCENT_COLOR,
        )
        self._rebuild_model_rows()

    def _render_active(self) -> None:
        session = self.manager.active
        self.transcript.configure(state=tk.NORMAL)
        self.transcript.delete("1.0", tk.END)
        if session.messages or session.streaming:
            self.welcome_panel.grid_remove()
            self.transcript.grid()
            for speaker, text in session.messages:
                self._insert_message(speaker, text)
            if session.streaming:
                self._insert_message("MyAgent", session.streaming_text)
        else:
            self.transcript.grid_remove()
            self.welcome_panel.grid()
        self.transcript.configure(state=tk.DISABLED)
        if self.input.get("1.0", "end-1c") != session.draft:
            self.input.delete("1.0", tk.END)
            self.input.insert("1.0", session.draft)
        selected_model = session.controller.model
        if selected_model:
            self.model_value.set(selected_model)
        elif self.manager.models:
            self.model_value.set("选择模型")
        else:
            self.model_value.set("尚无模型")
        self.kind_value.set(AGENT_KIND_LABELS[session.controller.kind])
        self.workspace_value.set(str(session.workspace))
        workspace_name = session.workspace.name or session.workspace.drive
        self.header_workspace_value.set(f"LOCAL · {workspace_name}")
        self.status.set(session.status)
        self._update_controls()

    def _insert_message(self, speaker: str, text: str) -> None:
        self.transcript.insert(tk.END, f"{speaker}\n", "speaker")
        tag = "error" if speaker == "错误" else "message"
        self.transcript.insert(tk.END, f"{text}\n", tag)
        self.transcript.see(tk.END)

    def _refresh_model_choices(self) -> None:
        choices = list(self.manager.model_names)
        self.model_selector.configure(values=choices or ["尚无模型"])
        self._rebuild_model_rows()

    def _rebuild_model_rows(self) -> None:
        for child in self._model_row_widgets:
            child.destroy()
        self._model_row_widgets.clear()
        if not self.manager.models:
            empty = ctk.CTkFrame(
                self.model_rows,
                fg_color=_PANEL_BACKGROUND,
                border_width=1,
                border_color=_BORDER_COLOR,
                corner_radius=12,
                height=180,
            )
            empty.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
            empty.grid_propagate(False)
            empty.columnconfigure(0, weight=1)
            ctk.CTkLabel(
                empty,
                text="还没有可用模型",
                text_color=_TEXT_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CARD_TITLE, "bold"),
            ).grid(row=0, column=0, pady=(38, 4))
            ctk.CTkLabel(
                empty,
                text="添加模型名称、API key 和可选 Base URL 后，即可在每个会话中独立选择。",
                text_color=_MUTED_TEXT_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            ).grid(row=1, column=0)
            ctk.CTkButton(
                empty,
                text="添加第一个模型",
                command=self.open_register_dialog,
                width=142,
                height=36,
                corner_radius=9,
                fg_color=_RAISED_BACKGROUND,
                hover_color="#303a40",
                text_color=_TEXT_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
            ).grid(row=2, column=0, pady=(18, 30))
            self._model_row_widgets.append(empty)
            return
        for row, record in enumerate(self.manager.models):
            created_at = record.created_at.replace("T", " ").removesuffix("Z")
            created_at = created_at.split(".", 1)[0]
            panel = ctk.CTkFrame(
                self.model_rows,
                fg_color=_PANEL_BACKGROUND,
                border_width=1,
                border_color=_BORDER_COLOR,
                corner_radius=12,
            )
            panel.grid(row=row, column=0, padx=6, pady=(0, 10), sticky="ew")
            panel.columnconfigure(0, weight=1)
            ctk.CTkLabel(
                panel,
                text=record.name,
                text_color=_TEXT_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CARD_TITLE, "bold"),
                anchor="w",
            ).grid(row=0, column=0, padx=(18, 8), pady=(14, 1), sticky="w")
            ctk.CTkLabel(
                panel,
                text=record.base_url or "使用全局或 OpenAI 默认地址",
                text_color=_MUTED_TEXT_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
                anchor="w",
            ).grid(row=1, column=0, padx=(18, 8), pady=(0, 2), sticky="w")
            ctk.CTkLabel(
                panel,
                text=f"凭据已保存  ·  {created_at}",
                text_color=_MUTED_TEXT_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
                anchor="w",
            ).grid(row=2, column=0, padx=(18, 8), pady=(0, 14), sticky="w")
            ctk.CTkButton(
                panel,
                text="删除",
                command=lambda name=record.name: self.delete_model(name),
                width=72,
                height=34,
                corner_radius=8,
                fg_color="transparent",
                hover_color=_DANGER_HOVER_COLOR,
                border_width=1,
                border_color="#573035",
                text_color=_ERROR_COLOR,
                font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            ).grid(row=0, column=1, rowspan=3, padx=(10, 16), pady=16)
            self._model_row_widgets.append(panel)

    def open_register_dialog(self) -> None:
        dialog = ctk.CTkToplevel(self.root, fg_color=_APP_BACKGROUND)
        dialog.title("添加模型")
        dialog.geometry("480x500")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        dialog.after(20, dialog.grab_set)

        frame = ctk.CTkFrame(
            dialog,
            fg_color=_APP_BACKGROUND,
            corner_radius=0,
        )
        frame.grid(row=0, column=0, padx=28, pady=24, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            frame,
            text="添加模型",
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_DIALOG_TITLE, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            frame,
            text="凭据保存在用户配置目录，不会出现在模型列表或对话记录中。",
            text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="w",
            wraplength=410,
        ).grid(row=1, column=0, pady=(4, 20), sticky="w")

        ctk.CTkLabel(
            frame,
            text="模型名称",
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL, "bold"),
            anchor="w",
        ).grid(row=2, column=0, sticky="w")
        name = tk.StringVar()
        name_entry = ctk.CTkEntry(
            frame,
            textvariable=name,
            placeholder_text="例如 gpt-5.6-sol",
            height=42,
            corner_radius=9,
            fg_color=_INPUT_BACKGROUND,
            border_color=_BORDER_COLOR,
            text_color=_TEXT_COLOR,
            placeholder_text_color=_MUTED_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
        )
        name_entry.grid(row=3, column=0, sticky="ew", pady=(7, 16))
        ctk.CTkLabel(
            frame,
            text="Base URL（可选）",
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL, "bold"),
            anchor="w",
        ).grid(row=4, column=0, sticky="w")
        base_url = tk.StringVar()
        base_url_entry = ctk.CTkEntry(
            frame,
            textvariable=base_url,
            placeholder_text="例如 https://api.example.com/v1",
            height=42,
            corner_radius=9,
            fg_color=_INPUT_BACKGROUND,
            border_color=_BORDER_COLOR,
            text_color=_TEXT_COLOR,
            placeholder_text_color=_MUTED_TEXT_COLOR,
            font=(_MONO_FONT_FAMILY, _FONT_SIZE_CONTROL),
        )
        base_url_entry.grid(row=5, column=0, sticky="ew", pady=(7, 16))
        ctk.CTkLabel(
            frame,
            text="API key",
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL, "bold"),
            anchor="w",
        ).grid(row=6, column=0, sticky="w")
        key = tk.StringVar()
        key_entry = ctk.CTkEntry(
            frame,
            textvariable=key,
            show="•",
            placeholder_text="输入密钥",
            height=42,
            corner_radius=9,
            fg_color=_INPUT_BACKGROUND,
            border_color=_BORDER_COLOR,
            text_color=_TEXT_COLOR,
            placeholder_text_color=_MUTED_TEXT_COLOR,
            font=(_MONO_FONT_FAMILY, _FONT_SIZE_CONTROL),
        )
        key_entry.grid(row=7, column=0, sticky="ew", pady=(7, 8))
        error = tk.StringVar()
        ctk.CTkLabel(
            frame,
            textvariable=error,
            text_color=_ERROR_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CAPTION),
            anchor="w",
        ).grid(
            row=8,
            column=0,
            sticky="w",
            pady=(0, 8),
        )

        def confirm() -> None:
            try:
                self.manager.register_model(name.get(), key.get(), base_url.get())
            except ModelStoreError as exc:
                error.set(str(exc))
                return
            key.set("")
            dialog.destroy()
            self._refresh_model_choices()
            self.model_page_status.set("模型已注册")

        actions = ctk.CTkFrame(frame, fg_color="transparent")
        actions.grid(row=9, column=0, pady=(8, 0), sticky="e")
        ctk.CTkButton(
            actions,
            text="取消",
            command=dialog.destroy,
            width=84,
            height=38,
            corner_radius=9,
            fg_color="transparent",
            hover_color=_RAISED_BACKGROUND,
            border_width=1,
            border_color=_BORDER_COLOR,
            text_color=_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL),
        ).grid(row=0, column=0, padx=(0, 9))
        ctk.CTkButton(
            actions,
            text="保存模型",
            command=confirm,
            width=106,
            height=38,
            corner_radius=9,
            fg_color=_ACCENT_COLOR,
            hover_color=_ACCENT_HOVER_COLOR,
            text_color=_ACCENT_TEXT_COLOR,
            font=(_FONT_FAMILY, _FONT_SIZE_CONTROL, "bold"),
        ).grid(row=0, column=1)
        dialog.bind("<Return>", lambda _event: confirm())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        name_entry.focus_set()

    def delete_model(self, name: str) -> None:
        if not messagebox.askyesno(
            "删除模型配置",
            f"确定删除模型“{name}”吗？",
            parent=self.root,
            default=messagebox.NO,
        ):
            return
        try:
            deleted = self.manager.delete_model(name)
        except ModelStoreError as exc:
            self.model_page_status.set(str(exc))
            return
        if not deleted:
            self.model_page_status.set(self.manager.last_error)
            return
        self._refresh_model_choices()
        self._render_active()
        self.model_page_status.set("模型配置已删除")

    def _on_model_selected(self, _selection: object | None = None) -> None:
        name = self.model_value.get()
        if name not in self.manager.model_names:
            self.status.set("请先前往模型页添加并选择模型")
            return
        session = self.manager.active
        if not self.manager.select_model(session.session_id, name):
            self.model_value.set(session.controller.model or "选择模型")
            self.status.set(self.manager.last_error)
            return
        session.status = f"模型：{name}"
        self.status.set(session.status)
        self._update_controls()

    def _on_kind_selected(self, _selection: object | None = None) -> None:
        session = self.manager.active
        kind = LABEL_AGENT_KINDS[self.kind_value.get()]
        if not self.manager.set_kind(session.session_id, kind):
            self.kind_value.set(AGENT_KIND_LABELS[session.controller.kind])
            self.status.set(self.manager.last_error)
            return
        session.status = f"Agent：{AGENT_KIND_LABELS[kind]}"
        self.status.set(session.status)

    def _on_ctrl_enter(self, _event: tk.Event[tk.Misc]) -> str:
        self.send()
        return "break"

    def send(self) -> None:
        session = self.manager.active
        prompt = self.input.get("1.0", "end-1c").strip()
        if not session.controller.submit(prompt):
            self.status.set(session.controller.last_error)
            return
        session.messages.append(("你", prompt))
        session.draft = ""
        session.status = f"正在思考 · {session.controller.model}"
        self.input.delete("1.0", tk.END)
        self._render_active()

    def _update_controls(self) -> None:
        session = self.manager.active
        idle = session.controller.state == "idle" and not self._closing_all
        can_send = idle and bool(session.controller.model)
        self.send_button.configure(
            state="normal" if can_send else "disabled",
            fg_color=_ACCENT_COLOR if can_send else _ACCENT_DISABLED_COLOR,
        )
        model_state = "normal" if idle and self.manager.models else "disabled"
        self.model_selector.configure(state=model_state)
        kind_state = "normal" if idle and not session.controller.has_started else "disabled"
        self.kind_selector.configure(state=kind_state)
        self.input.configure(state="disabled" if self._closing_all else "normal")
        if session.controller.state == "busy":
            self.status_label.configure(text_color=_ACCENT_COLOR)
        elif "失败" in session.status or self.manager.last_error:
            self.status_label.configure(text_color=_ERROR_COLOR)
        else:
            self.status_label.configure(text_color=_MUTED_TEXT_COLOR)

    def _poll_events(self) -> None:
        if self._destroyed:
            return
        if self._poll_after_id is not None:
            try:
                self.root.after_cancel(self._poll_after_id)
            except tk.TclError:
                pass
            self._poll_after_id = None
        changed_active = False
        for session in self.manager.sessions:
            while True:
                try:
                    event = session.controller.events.get_nowait()
                except queue.Empty:
                    break
                if event.kind == "approval":
                    self._handle_approval(session, event.payload)
                    continue
                if event.kind == "stream_start":
                    session.streaming = True
                    session.streaming_text = ""
                    if session.session_id == self.manager.active_session_id:
                        changed_active = True
                    continue
                if event.kind == "stream_delta":
                    session.streaming = True
                    session.streaming_text += str(event.payload)
                    if session.session_id == self.manager.active_session_id:
                        changed_active = True
                    continue
                close_ready = session.controller.consume_result()
                session.streaming = False
                session.streaming_text = ""
                if event.kind == "assistant":
                    session.messages.append(("MyAgent", str(event.payload)))
                    session.status = f"就绪 · {session.controller.model}"
                else:
                    session.messages.append(("错误", str(event.payload)))
                    session.status = "请求失败，可继续发送"
                if not self.manager.persist_completed_turn(
                    session.session_id
                ):
                    session.status = self.manager.conversation_error or self.manager.last_error
                if close_ready:
                    session.controller.close_agent_once()
                if session.session_id == self.manager.active_session_id:
                    changed_active = True
        if changed_active and not self._closing_all:
            self._save_draft()
            self._render_active()
        if self._closing_all and self.manager.all_closed():
            self._finish_close()
            return
        self._poll_after_id = self.root.after(self._POLL_MS, self._poll_events)

    def _handle_approval(
        self,
        session: ConversationSession,
        payload: object,
    ) -> None:
        if not isinstance(payload, ApprovalProposal):
            return
        if self._closing_all or session.controller.state == "closing":
            session.controller.approvals.decide(payload, False)
            return
        reason = session.controller.redact_for_ui(payload.reason)
        arguments = session.controller.redact_for_ui(
            json.dumps(payload.arguments, ensure_ascii=False, indent=2)
        )
        approved = messagebox.askyesno(
            "需要确认敏感操作",
            f"标签：{session.title}\n工具：{payload.tool_name}\n\n"
            f"原因：{reason}\n\n参数：\n{arguments}",
            parent=self.root,
            default=messagebox.NO,
        )
        session.controller.approvals.decide(payload, approved)

    def close(self) -> None:
        if self._destroyed or self._closing_all:
            return
        self._closing_all = True
        self._update_controls()
        if self.manager.begin_close_all():
            self._finish_close()
        else:
            self.status.set("正在安全关闭运行中的对话…")

    def _finish_close(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        pending_callbacks = self.root.tk.splitlist(
            self.root.tk.call("after", "info")
        )
        for callback_id in pending_callbacks:
            try:
                # Cancel the Tcl timer without deleting its Python command.
                # Each owning widget removes that command during destroy().
                self.root.tk.call("after", "cancel", callback_id)
            except tk.TclError:
                pass
        self._poll_after_id = None
        self.root.destroy()


def main() -> None:
    """Open immediately; credentials and Agent runtimes remain lazy."""
    _set_windows_app_id()
    root = ctk.CTk(fg_color=_APP_BACKGROUND)
    MyAgentWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
