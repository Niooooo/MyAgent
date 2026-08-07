"""Newline-delimited desktop bridge for the Electron renderer."""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any, Callable, TextIO

from .gui import ApprovalProposal, SessionManager


ProtocolPayload = dict[str, Any]
EventEmitter = Callable[[str, ProtocolPayload], None]


class ProtocolError(ValueError):
    """A stable request validation failure safe to return to the desktop UI."""


class JsonLineWriter:
    """Serialize protocol messages without interleaving worker output."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, payload: ProtocolPayload) -> None:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


class DesktopSidecar:
    """Expose headless GUI ownership through a narrow desktop protocol."""

    _POLL_SECONDS = 0.05

    def __init__(
        self,
        manager: SessionManager | None = None,
        *,
        emit: EventEmitter | None = None,
        start_polling: bool = True,
    ) -> None:
        self.manager = manager or SessionManager()
        self._emit = emit or (lambda _event, _payload: None)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._closed = False
        self._closing = False
        self._approval_ids = count(1)
        self._approvals: dict[str, tuple[int, ApprovalProposal]] = {}
        self._poll_thread: threading.Thread | None = None
        if start_polling:
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                name="myagent-desktop-events",
                daemon=True,
            )
            self._poll_thread.start()

    def dispatch(self, method: str, params: object | None = None) -> ProtocolPayload:
        if not isinstance(method, str) or not method:
            raise ProtocolError("method must be a non-empty string")
        values = {} if params is None else params
        if not isinstance(values, dict):
            raise ProtocolError("params must be an object")
        with self._lock:
            if self._closed:
                raise ProtocolError("desktop sidecar is closed")
            handlers: dict[str, Callable[[dict[str, Any]], ProtocolPayload]] = {
                "app.snapshot": self._snapshot_request,
                "app.shutdown": self._shutdown_request,
                "session.new": self._new_session,
                "session.activate": self._activate_session,
                "session.close": self._close_session,
                "session.select_model": self._select_model,
                "session.set_kind": self._set_kind,
                "session.submit": self._submit,
                "model.register": self._register_model,
                "model.delete": self._delete_model,
                "model.refresh": self._refresh_models,
                "approval.decide": self._decide_approval,
            }
            handler = handlers.get(method)
            if handler is None:
                raise ProtocolError(f"unknown method: {method}")
            return handler(values)

    def snapshot(self) -> ProtocolPayload:
        with self._lock:
            sessions = [self._session_snapshot(session) for session in self.manager.sessions]
            return {
                "activeSessionId": self.manager.active_session_id,
                "sessions": sessions,
                "models": [
                    {
                        "name": model.name,
                        "baseUrl": model.base_url,
                        "createdAt": model.created_at,
                    }
                    for model in self.manager.models
                ],
                "modelError": self.manager.model_error,
                "lastError": self.manager.last_error,
                "closing": self._closing,
            }

    def poll_once(self) -> bool:
        changed = False
        with self._lock:
            for session in tuple(self.manager.sessions):
                while True:
                    try:
                        event = session.controller.events.get_nowait()
                    except queue.Empty:
                        break
                    if event.kind == "approval":
                        if isinstance(event.payload, ApprovalProposal):
                            self._publish_approval(session.session_id, event.payload)
                        continue
                    if event.kind == "stream_start":
                        session.streaming = True
                        session.streaming_text = ""
                        self._emit(
                            "stream-start",
                            {"sessionId": session.session_id},
                        )
                        continue
                    if event.kind == "stream_delta":
                        delta = str(event.payload)
                        session.streaming = True
                        session.streaming_text += delta
                        self._emit(
                            "stream-delta",
                            {"sessionId": session.session_id, "delta": delta},
                        )
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
                    if close_ready:
                        session.controller.close_agent_once()
                    changed = True
            if changed:
                self._emit("state", self.snapshot())
            if self._closing and self.manager.all_closed():
                self._emit("shutdown-ready", {})
        return changed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closing = True
            self.manager.begin_close_all()
        while not self.manager.all_closed():
            self.poll_once()
            time.sleep(self._POLL_SECONDS)
        self._stop.set()
        thread = self._poll_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self._lock:
            self._approvals.clear()
            self._closed = True

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._POLL_SECONDS):
            try:
                self.poll_once()
            except Exception as exc:
                self._emit("sidecar-error", {"message": f"桌面事件轮询失败：{exc}"})

    def _snapshot_request(self, _params: dict[str, Any]) -> ProtocolPayload:
        return {"state": self.snapshot()}

    def _shutdown_request(self, _params: dict[str, Any]) -> ProtocolPayload:
        self._closing = True
        self.manager.begin_close_all()
        return {"state": self.snapshot()}

    def _new_session(self, params: dict[str, Any]) -> ProtocolPayload:
        cwd = params.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ProtocolError("cwd must be a string")
        self.manager.new_session(cwd or None)
        return {"state": self.snapshot()}

    def _activate_session(self, params: dict[str, Any]) -> ProtocolPayload:
        session_id = self._session_id(params)
        if not self.manager.activate(session_id):
            raise ProtocolError("conversation does not exist")
        return {"state": self.snapshot()}

    def _close_session(self, params: dict[str, Any]) -> ProtocolPayload:
        session_id = self._session_id(params)
        if not self.manager.close_session(session_id):
            raise ProtocolError(self.manager.last_error or "conversation could not be closed")
        return {"state": self.snapshot()}

    def _select_model(self, params: dict[str, Any]) -> ProtocolPayload:
        session_id = self._session_id(params)
        name = self._required_text(params, "name")
        if not self.manager.select_model(session_id, name):
            raise ProtocolError(self.manager.last_error or "model could not be selected")
        session = self._find_session(session_id)
        session.status = f"模型：{name}"
        return {"state": self.snapshot()}

    def _set_kind(self, params: dict[str, Any]) -> ProtocolPayload:
        session = self._find_session(self._session_id(params))
        kind = self._required_text(params, "kind")
        if kind not in {"main", "sub"}:
            raise ProtocolError("kind must be 'main' or 'sub'")
        if not session.controller.set_kind(kind):
            raise ProtocolError(session.controller.last_error or "Agent type could not be changed")
        session.status = "Agent：主 Agent" if kind == "main" else "Agent：子 Agent"
        return {"state": self.snapshot()}

    def _submit(self, params: dict[str, Any]) -> ProtocolPayload:
        session = self._find_session(self._session_id(params))
        prompt = self._required_text(params, "prompt")
        if not session.controller.submit(prompt):
            raise ProtocolError(session.controller.last_error or "message could not be sent")
        session.messages.append(("你", prompt.strip()))
        session.draft = ""
        session.status = f"正在思考 · {session.controller.model}"
        return {"state": self.snapshot()}

    def _register_model(self, params: dict[str, Any]) -> ProtocolPayload:
        name = self._required_text(params, "name")
        api_key = self._required_text(params, "apiKey", strip=False)
        base_url = self._optional_text(params, "baseUrl")
        self.manager.register_model(name, api_key, base_url)
        return {"state": self.snapshot()}

    def _delete_model(self, params: dict[str, Any]) -> ProtocolPayload:
        name = self._required_text(params, "name")
        if not self.manager.delete_model(name):
            raise ProtocolError(self.manager.last_error or "model could not be deleted")
        return {"state": self.snapshot()}

    def _refresh_models(self, _params: dict[str, Any]) -> ProtocolPayload:
        if not self.manager.refresh_models():
            raise ProtocolError(self.manager.model_error or "models could not be refreshed")
        return {"state": self.snapshot()}

    def _decide_approval(self, params: dict[str, Any]) -> ProtocolPayload:
        approval_id = self._required_text(params, "approvalId")
        approved = params.get("approved")
        if not isinstance(approved, bool):
            raise ProtocolError("approved must be a boolean")
        pending = self._approvals.pop(approval_id, None)
        if pending is None:
            raise ProtocolError("approval is no longer pending")
        session_id, proposal = pending
        session = self._find_session(session_id)
        session.controller.approvals.decide(proposal, approved)
        return {"accepted": True}

    def _publish_approval(self, session_id: int, proposal: ApprovalProposal) -> None:
        session = self._find_session(session_id)
        approval_id = f"approval-{next(self._approval_ids)}"
        self._approvals[approval_id] = (session_id, proposal)
        arguments = json.dumps(proposal.arguments, ensure_ascii=False, indent=2)
        self._emit(
            "approval",
            {
                "approvalId": approval_id,
                "sessionId": session_id,
                "toolName": proposal.tool_name,
                "reason": session.controller.redact_for_ui(proposal.reason),
                "arguments": session.controller.redact_for_ui(arguments),
            },
        )

    def _session_snapshot(self, session: Any) -> ProtocolPayload:
        controller = session.controller
        return {
            "id": session.session_id,
            "title": session.title,
            "workspace": str(session.workspace),
            "workspaceName": session.workspace.name or session.workspace.drive,
            "model": controller.model,
            "kind": controller.kind,
            "state": controller.state,
            "hasStarted": controller.has_started,
            "messages": [
                {"speaker": speaker, "text": text}
                for speaker, text in session.messages
            ],
            "status": session.status,
            "canSend": controller.state == "idle" and bool(controller.model),
            "canChangeKind": controller.state == "idle" and not controller.has_started,
        }

    def _find_session(self, session_id: int) -> Any:
        for session in self.manager.sessions:
            if session.session_id == session_id:
                return session
        raise ProtocolError("conversation does not exist")

    @staticmethod
    def _session_id(params: dict[str, Any]) -> int:
        value = params.get("sessionId")
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError("sessionId must be an integer")
        return value

    @staticmethod
    def _required_text(
        params: dict[str, Any],
        name: str,
        *,
        strip: bool = True,
    ) -> str:
        value = params.get(name)
        if not isinstance(value, str):
            raise ProtocolError(f"{name} must be a string")
        normalized = value.strip() if strip else value
        if not normalized:
            raise ProtocolError(f"{name} must not be empty")
        return normalized

    @staticmethod
    def _optional_text(params: dict[str, Any], name: str) -> str | None:
        value = params.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ProtocolError(f"{name} must be a string")
        return value.strip() or None


def serve(stdin: TextIO, stdout: TextIO) -> None:
    writer = JsonLineWriter(stdout)
    sidecar = DesktopSidecar(
        emit=lambda event, payload: writer.write({"event": event, "payload": payload})
    )
    writer.write({"event": "state", "payload": sidecar.snapshot()})
    try:
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            request_id: object = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ProtocolError("request must be an object")
                request_id = request.get("id")
                if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
                    raise ProtocolError("request id must be a string or integer")
                result = sidecar.dispatch(request.get("method"), request.get("params"))
            except Exception as exc:
                writer.write(
                    {
                        "id": request_id,
                        "error": {
                            "code": type(exc).__name__,
                            "message": str(exc) or type(exc).__name__,
                        },
                    }
                )
            else:
                writer.write({"id": request_id, "result": result})
    finally:
        sidecar.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="MyAgent Electron sidecar")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.check:
        print(f"MyAgent desktop sidecar check passed ({Path.cwd()})")
        return
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr
    serve(sys.stdin, protocol_stdout)


if __name__ == "__main__":
    main()
