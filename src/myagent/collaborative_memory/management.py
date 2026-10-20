"""Human application commands; result feedback is never a model success tool."""

from __future__ import annotations

from pathlib import Path

from .storage import project_identity
from .store import MemoryStore


def manage(workspace: Path, action: str, values: dict, *, service=None) -> dict:
    store = service.store if service is not None else None
    owned = store is None
    if owned:
        project, _revision = project_identity(Path(workspace))
        if not (project / ".myagent" / "collaborative-memory" / "memory.sqlite3").exists():
            if action == "status":
                return {"mode": "off" if service is None else service.config.mode,
                        "episodes": [], "insights": [], "jobs": [], "errors": []}
            raise ValueError("this project has no collaborative memory yet")
        store = MemoryStore(Path(workspace))
    try:
        if action == "status":
            return service.status() if service is not None and service.store else {
                "episodes": store.rows("SELECT * FROM episodes ORDER BY rowid DESC LIMIT 30"),
                "insights": store.rows("SELECT * FROM insights ORDER BY rowid DESC LIMIT 50"),
                "jobs": store.rows("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30"),
            }
        if action == "sources":
            return store.sources(values["episode_id"])
        if action == "feedback":
            evidence = store.feedback(
                values["episode_id"], criterion=values["criterion"], passed=values["passed"],
                summary=values["summary"], source="user", revision=values.get("revision"),
                evidence_id=values.get("evidence_id"),
            )
            if service:
                service.wake()
            return {"ok": True, "evidence_id": evidence, "episode": store.episode(values["episode_id"])}
        if action in {"disable", "delete", "revise"}:
            store.change_insight(values["insight_id"], action, reason=values["reason"],
                                 evidence_id=values.get("evidence_id"), text=values.get("text"))
        elif action == "delete_episode":
            store.delete_episode(values["episode_id"])
        elif action == "rebuild":
            store.rebuild_projections()
        elif action == "recover":
            store.recover_interrupted()
        elif action == "end":
            store.end(values["episode_id"], cancelled=values.get("cancelled", False))
        elif action == "retry":
            with store.transaction() as db:
                db.execute("UPDATE jobs SET state='pending',attempts=0,lease_until=0 WHERE state='failed'")
            if service:
                service.wake()
        else:
            raise ValueError("unknown collaborative memory command")
        return {"ok": True, "action": action}
    finally:
        if owned:
            store.close()
