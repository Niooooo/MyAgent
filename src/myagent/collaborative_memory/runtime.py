"""Composition-owned service; per-run identities never follow mutable member state."""

from __future__ import annotations

import contextvars
import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from .config import CollaborativeMemoryConfig
from .learning import Learner
from .retrieval import Retriever, field
from .storage import encode, identifier
from .store import MemoryStore


@dataclass(frozen=True)
class RunScope:
    episode_id: str
    run_id: str
    agent_id: str
    role: str
    work_id: str | None = None
    task_id: str | None = None


class CollaborativeMemory:
    def __init__(self, workspace: Path, config: CollaborativeMemoryConfig, *,
                 client=None, model: str = "", allowed_tools=None) -> None:
        self.config, self.client, self.model = config, client, model
        if config.mode == "learn" and callable(getattr(client, "with_options", None)):
            self.client = client.with_options(max_retries=0, timeout=config.timeout_seconds)
        self.allowed_tools = allowed_tools
        self.workspace = Path(workspace).resolve()
        self.store: MemoryStore | None = None
        self.errors: list[dict] = []
        self.last_episode: str | None = None
        self.retrieval_variant = "full"
        self._context = contextvars.ContextVar("collaborative_run", default=None)
        self._lock = RLock()
        self._learner = None
        if config.mode != "off":
            try:
                self.store = MemoryStore(self.workspace)
                if config.mode == "learn" and client is not None:
                    self._learner = Learner(self)
            except Exception as exc:
                self.report("storage", exc)

    def report(self, stage: str, exc: Exception) -> None:
        message = {"stage": stage, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        with self._lock:
            self.errors.append(message)
            self.errors[:] = self.errors[-20:]
        warnings.warn(f"Collaborative memory {stage}: {message['error']}", RuntimeWarning, stacklevel=2)

    def observer(self, role: str, agent_id: str | None = None, workspace=None):
        if self.config.mode == "off":
            return None
        return RunObserver(self, role, agent_id or identifier("agent_"), workspace or self.workspace)

    def current_scope(self):
        return self._context.get()

    def submit(self, executor, callback, *args, task_id: str | None = None, origin=None):
        """Reserve work before submitting, including queued work and cancelled futures."""
        scope = origin or self._context.get()
        if scope is None or self.store is None:
            return executor.submit(callback, *args)
        work_id = self.store.reserve(scope.episode_id, scope.run_id, task_id)
        delegated = RunScope(scope.episode_id, scope.run_id, scope.agent_id, scope.role, work_id, task_id)

        def run():
            token = self._context.set(delegated)
            try:
                return callback(*args)
            finally:
                self._context.reset(token)
                self._finish_work(work_id)
        try:
            future = executor.submit(run)
        except BaseException:
            self._finish_work(work_id)
            raise
        future.add_done_callback(lambda done: self._finish_work(work_id) if done.cancelled() else None)
        return future

    def _finish_work(self, work_id: str) -> None:
        try:
            self.store.finish_work(work_id)
            self.wake()
        except Exception as exc:
            self.report("completion", exc)

    def wake(self) -> None:
        if self._learner:
            self._learner.wake()

    def message_sent(self, payload: dict) -> dict:
        scope = self._context.get()
        if scope is None or self.store is None:
            return payload
        payload = dict(payload, episode_id=scope.episode_id, run_id=scope.run_id)
        try:
            sender = payload["sender"]
            run_id = scope.run_id
            role = scope.role
            if sender != scope.agent_id:
                runs = self.store.rows(
                    "SELECT id,role FROM runs WHERE episode_id=? AND agent_id=? ORDER BY rowid DESC LIMIT 1",
                    (scope.episode_id, sender),
                )
                if runs:
                    run_id = runs[0]["id"]
                    role = runs[0]["role"]
            payload["run_id"] = run_id
            self.store.event(scope.episode_id, "message:" + payload["id"], "message_sent",
                             encode(payload), run_id=run_id,
                             agent_id=sender, role=role)
        except Exception as exc:
            self.report("message", exc)
        return payload

    def message_delivered(self, payload: dict, recipient: str) -> bool:
        if self.store is None or not payload.get("episode_id"):
            return True
        try:
            eid = payload["episode_id"]
            sent = self.store.event(eid, "message:" + payload["id"], "message_sent", encode(payload),
                                    run_id=payload.get("run_id"), agent_id=payload["sender"])
            delivered = self.store.event(eid, "delivery:" + payload["id"], "message_delivered",
                                         encode({"message_id": payload["id"], "recipient": recipient}),
                                         agent_id=recipient)
            self.store.edge(sent, delivered, "delivery", eid, payload["id"])
            return True
        except Exception as exc:
            self.report("message_delivery", exc)
            return False  # retain inbox file when durable collection failed

    def status(self) -> dict:
        result = {"mode": self.config.mode, "errors": list(self.errors), "last_episode": self.last_episode}
        if self.store:
            result["jobs"] = self.store.rows("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30")
            result["episodes"] = self.store.rows("SELECT * FROM episodes ORDER BY rowid DESC LIMIT 30")
            result["insights"] = self.store.rows("SELECT * FROM insights ORDER BY rowid DESC LIMIT 50")
        return result

    def close(self) -> None:
        if self._learner:
            self._learner.close()
        if self.store:
            self.store.close()


class RunObserver:
    def __init__(self, service: CollaborativeMemory, role: str, agent_id: str, workspace) -> None:
        self.service, self.role, self.agent_id, self.workspace = service, role, agent_id, workspace
        self.scope = None
        self.token = None
        self.timeline = None
        self.root = False
        self.last_episode = None
        self._insight_versions = {}
        self._source_versions = {}
        self._calls = {}

    def begin(self, task: str, timeline, *, episode_id=None, criteria=None, tools=()) -> str:
        service, store = self.service, self.service.store
        if store is None:
            return ""
        parent = service._context.get()
        self.root = parent is None
        if parent is None and episode_id is not None:
            store.episode(episode_id)  # explicit continuation errors are not silent fallbacks
        if criteria is not None and (not isinstance(criteria, list) or any(
            not isinstance(c, str) or not c.strip() for c in criteria
        )):
            raise ValueError("acceptance criteria must be non-empty strings")
        try:
            eid = (parent.episode_id if parent else store.begin(task, criteria or [], episode_id=episode_id))
            rid = identifier("run_")
            self.scope = RunScope(eid, rid, self.agent_id, self.role,
                                  parent.work_id if parent else None, parent.task_id if parent else None)
            store.start_run(eid, rid, self.agent_id, self.role, parent.run_id if parent else None,
                            parent.task_id if parent else None,
                            str(getattr(self.workspace, "current", self.workspace)))
            self.last_episode = eid
            if self.root:
                service.last_episode = eid
            self.token = service._context.set(self.scope)
            self.timeline = timeline
            timeline.add_observer(self.capture)
            if service.config.mode == "learn":
                package, _trace = Retriever(store, service.config, service.client, service.model,
                                            service.allowed_tools, variant=service.retrieval_variant).retrieve(
                                                eid, task, self.role, list(tools))
                self._insight_versions = {
                    row["id"]: row["version"] for row in store.rows(
                        "SELECT i.id,i.version FROM insights i JOIN usage u ON u.insight_id=i.id "
                        "WHERE u.episode_id=? AND u.stage='provided'", (eid,)
                    )
                }
                self._source_versions = {
                    qid: store.episode(qid)["version"] for qid in _trace.provided_tasks
                }
                return package
        except Exception as exc:
            service.report("begin", exc)
        return ""

    def context_is_current(self) -> bool:
        if self.service.store is None:
            return False
        try:
            for qid, version in self._source_versions.items():
                if self.service.store.episode(qid)["version"] != version:
                    return False
            for iid, version in self._insight_versions.items():
                item = self.service.store.insight(iid)
                if item["status"] != "active" or item["version"] != version:
                    return False
            return True
        except Exception as exc:
            self.service.report("context", exc)
            return False

    def capture(self, operation) -> None:
        if self.scope is None or operation.kind in {"request_succeeded", "restore", "reset", "compaction", "emergency_compaction"}:
            return
        for index, item in enumerate(operation.items):
            kind = field(item, "type", field(item, "role", ""))
            if kind == "reasoning":
                continue
            # Preserve only observable fields; never serialize hidden reasoning.
            if kind == "function_call":
                body = encode({k: field(item, k) for k in ("name", "arguments", "call_id")})
            elif kind == "function_call_output":
                body = encode({k: field(item, k) for k in ("call_id", "output")})
            else:
                content = field(item, "content", "")
                if not isinstance(content, str):
                    content = "\n".join(str(field(part, "text", "")) for part in content or []
                                        if field(part, "type") in {"output_text", "input_text", "text"})
                body = content
            if not body or body.startswith("COLLABORATIVE_MEMORY"):
                continue
            # Inbox relationships are persisted by the manager against the sending
            # episode, never relabel a late message as part of the receiving task.
            if body.startswith("AGENT_TEAM_INBOX"):
                continue
            eid = self.record(f"timeline:{operation.operation_id}:{index}", kind or operation.kind, body)
            if kind == "function_call" and eid:
                self._calls[field(item, "call_id")] = eid

    def record(self, key: str, kind: str, body: str) -> str | None:
        if self.scope is None:
            return
        scope = self.scope
        try:
            maximum = self.service.config.event_chars
            if len(body) > maximum:
                body = body[:maximum] + "\n[truncated by event budget]"
            return self.service.store.event(scope.episode_id, scope.run_id + ":" + key, kind, body,
                                            run_id=scope.run_id, agent_id=scope.agent_id, role=scope.role)
        except Exception as exc:
            self.service.report("capture", exc)

    def tool_result(self, call_id: str, result: dict) -> None:
        # Runs outside HookRegistry.emit; records the original before compaction.
        eid = self.record("result:" + call_id, "tool_result", encode({"call_id": call_id, "result": result}))
        if eid and call_id in self._calls:
            try:
                self.service.store.edge(self._calls[call_id], eid, "tool_result",
                                        self.scope.episode_id, call_id)
            except Exception as exc:
                self.service.report("tool_result", exc)

    def finish(self, output: str | None, failure: BaseException | None) -> None:
        try:
            if self.scope is not None:
                if output:
                    self.record("final", "assistant", output)
                self.service.store.finish_run(self.scope.episode_id, self.scope.run_id,
                                              root=self.root, interrupted=failure is not None)
                self.service.wake()
        except Exception as exc:
            self.service.report("finish", exc)
        finally:
            if self.timeline is not None:
                self.timeline.remove_observer(self.capture)
            if self.token is not None:
                self.service._context.reset(self.token)
            self.scope = self.token = self.timeline = None
            self._calls.clear()
