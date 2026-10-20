"""Task boundaries, observable interactions and explicit validation evidence."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .storage import Database, encode, identifier, project_identity


def event_id(key: str) -> str:
    return "e_" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex


class EpisodeStore(Database):
    def begin(self, task: str, criteria: list[str], *, episode_id: str | None = None) -> str:
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must be non-empty")
        if not isinstance(criteria, list) or any(
            not isinstance(item, str) or not item.strip() for item in criteria
        ) or len(set(criteria)) != len(criteria):
            raise ValueError("acceptance criteria must be distinct non-empty strings")
        _project, revision = project_identity(self.workspace)
        with self.transaction() as db:
            if episode_id:
                row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if row is None:
                    raise ValueError("unknown episode")
                if criteria and json.loads(row["criteria"]) != criteria:
                    raise ValueError("continued episodes retain their acceptance criteria")
                db.execute(
                    "UPDATE episodes SET lifecycle='running',root_done=0,version=version+1,revision=? WHERE id=?",
                    (revision, episode_id),
                )
                # A continued task must be verified again, even at the same Git HEAD.
                db.execute("UPDATE evidence SET active=0 WHERE episode_id=?", (episode_id,))
                db.execute("UPDATE episodes SET outcome='unknown' WHERE id=?", (episode_id,))
                db.execute("UPDATE jobs SET state='obsolete' WHERE episode_id=?", (episode_id,))
            else:
                episode_id = identifier("q_")
                db.execute(
                    "INSERT INTO episodes(id,task,workspace,revision,criteria) VALUES(?,?,?,?,?)",
                    (episode_id, task, str(self.workspace), revision, encode(criteria)),
                )
        return episode_id

    def episode(self, episode_id: str) -> dict:
        rows = self.rows("SELECT * FROM episodes WHERE id=?", (episode_id,))
        if not rows:
            raise ValueError("unknown episode")
        result = rows[0]
        result["criteria"] = json.loads(result["criteria"])
        return result

    def event(
        self, episode_id: str, key: str, kind: str, body: str, *,
        run_id: str | None = None, agent_id: str = "user", role: str = "user",
    ) -> str:
        eid = event_id(episode_id + ":" + key)
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
                return eid
            excerpt, blob = self.content(body)
            previous = db.execute(
                "SELECT id FROM events WHERE episode_id=? AND run_id IS ? ORDER BY ordinal DESC LIMIT 1",
                (episode_id, run_id),
            ).fetchone() if run_id else None
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM events").fetchone()[0]
            db.execute(
                "INSERT INTO events(id,episode_id,run_id,agent_id,role,kind,body,blob,ordinal) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (eid, episode_id, run_id, agent_id, role, kind, excerpt, blob, ordinal),
            )
            if kind != "verification":
                row = db.execute("SELECT lifecycle FROM episodes WHERE id=?", (episode_id,)).fetchone()
                if row[0] in {"ended", "interrupted"}:
                    db.execute("UPDATE episodes SET version=version+1,outcome='unknown' WHERE id=?", (episode_id,))
                    db.execute("UPDATE evidence SET active=0 WHERE episode_id=?", (episode_id,))
                    db.execute("UPDATE jobs SET state='obsolete' WHERE episode_id=?", (episode_id,))
                    self._reconcile(db, episode_id)
            if previous:
                self._edge(db, previous[0], eid, "sequence", episode_id, "same agent run")
        return eid

    @staticmethod
    def _edge(db, source: str, target: str, kind: str, via: str, reason: str) -> None:
        db.execute("INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                   (source, target, kind, via, reason))

    def edge(self, source: str, target: str, kind: str, via: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("graph edges require evidence")
        with self.transaction() as db:
            self._edge(db, source, target, kind, via, reason)

    def start_run(self, episode_id: str, run_id: str, agent_id: str, role: str,
                  parent: str | None, task_id: str | None, workspace: str) -> None:
        _project, revision = project_identity(Path(workspace))
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                       (run_id, episode_id, agent_id, role, parent, task_id, "running", workspace, revision))
            if parent:
                self._edge(db, parent, run_id, "delegation", episode_id, task_id or "delegated run")

    def reserve(self, episode_id: str, parent: str, task_id: str | None) -> str:
        wid = identifier("w_")
        with self.transaction() as db:
            db.execute("INSERT INTO work(id,episode_id,parent_run,task_id) VALUES(?,?,?,?)",
                       (wid, episode_id, parent, task_id))
            row = db.execute("SELECT lifecycle FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if row[0] in {"ended", "interrupted"}:
                db.execute("UPDATE episodes SET lifecycle='waiting',version=version+1,outcome='unknown' WHERE id=?",
                           (episode_id,))
                db.execute("UPDATE evidence SET active=0 WHERE episode_id=?", (episode_id,))
                db.execute("UPDATE jobs SET state='obsolete' WHERE episode_id=?", (episode_id,))
        return wid

    def finish_work(self, work_id: str) -> None:
        with self.transaction() as db:
            row = db.execute("SELECT episode_id FROM work WHERE id=?", (work_id,)).fetchone()
            if row:
                db.execute("UPDATE work SET state='ended' WHERE id=?", (work_id,))
                self._reconcile(db, row[0])

    def finish_run(self, episode_id: str, run_id: str, *, root: bool, interrupted: bool) -> None:
        with self.transaction() as db:
            db.execute("UPDATE runs SET state=? WHERE id=?",
                       ("interrupted" if interrupted else "ended", run_id))
            if root:
                db.execute(
                    "UPDATE episodes SET root_done=1,lifecycle=? WHERE id=?",
                    ("interrupted" if interrupted else "ended", episode_id),
                )
            self._reconcile(db, episode_id)

    def end(self, episode_id: str, *, cancelled: bool = False) -> None:
        self.episode(episode_id)
        with self.transaction() as db:
            db.execute("UPDATE episodes SET root_done=1,lifecycle=? WHERE id=?",
                       ("interrupted" if cancelled else "ended", episode_id))
            self._reconcile(db, episode_id)

    @staticmethod
    def _reconcile(db, episode_id: str) -> None:
        row = db.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
        pending = db.execute("SELECT 1 FROM work WHERE episode_id=? AND state='pending'",
                             (episode_id,)).fetchone()
        running = db.execute("SELECT 1 FROM runs WHERE episode_id=? AND state='running'",
                             (episode_id,)).fetchone()
        if row["root_done"] and row["lifecycle"] != "interrupted":
            db.execute("UPDATE episodes SET lifecycle=? WHERE id=?",
                       ("waiting" if pending or running else "ended", episode_id))
        if row["root_done"] and not pending and not running and row["outcome"] != "unknown":
            db.execute("INSERT OR IGNORE INTO jobs(episode_id,version) VALUES(?,?)",
                       (episode_id, row["version"]))

    def feedback(self, episode_id: str, *, criterion: str, passed: bool, summary: str,
                 source: str = "user", revision: str | None = None,
                 evidence_id: str | None = None) -> str:
        episode = self.episode(episode_id)
        if (source not in {"user", "validator"} or type(passed) is not bool
                or not isinstance(summary, str) or not summary.strip()):
            raise ValueError("explicit user or validator evidence is required")
        if criterion not in episode["criteria"]:
            raise ValueError("evidence must cover a declared acceptance criterion")
        if revision is not None and revision != episode["revision"]:
            raise ValueError("evidence belongs to a different artifact revision")
        if episode["lifecycle"] in {"running", "waiting"}:
            raise ValueError("finish related work before verifying this artifact version")
        eid = evidence_id or identifier("v_")
        recorded = self.rows("SELECT * FROM evidence WHERE id=?", (eid,))
        if recorded:
            if not recorded[0]["active"]:
                raise ValueError("evidence belongs to an earlier task version")
            expected = (episode_id, criterion, int(passed), source, summary)
            if tuple(recorded[0][key] for key in ("episode_id", "criterion", "passed", "source", "summary")) != expected:
                raise ValueError("evidence id already has different content")
            return eid
        event = self.event(episode_id, "verification:" + eid, "verification",
                           encode({"criterion": criterion, "passed": passed, "source": source,
                                   "summary": summary, "revision": episode["revision"]}))
        with self.transaction() as db:
            current = db.execute("SELECT version,lifecycle FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if current is None or current["version"] != episode["version"] or current["lifecycle"] in {"running", "waiting"}:
                raise ValueError("task changed while verification was being recorded")
            db.execute("INSERT INTO evidence(id,episode_id,criterion,passed,source,summary,revision,event_id) "
                       "VALUES(?,?,?,?,?,?,?,?)",
                       (eid, episode_id, criterion, int(passed), source, summary, episode["revision"], event))
            # Latest explicit evidence per criterion wins; historical evidence remains.
            evidence = db.execute(
                "SELECT criterion,passed FROM evidence WHERE episode_id=? AND active=1 ORDER BY rowid", (episode_id,)
            ).fetchall()
            results = {row[0]: bool(row[1]) for row in evidence}
            outcome = ("failed" if False in results.values() else
                       "success" if episode["criteria"] and all(results.get(c) for c in episode["criteria"])
                       else "unknown")
            db.execute("UPDATE episodes SET outcome=?,version=version+1 WHERE id=?", (outcome, episode_id))
            db.execute("UPDATE jobs SET state='obsolete' WHERE episode_id=?", (episode_id,))
            self._reconcile(db, episode_id)
        return eid

    def recover_interrupted(self) -> None:
        """Explicit recovery command; never interrupt another live desktop session."""
        with self.transaction() as db:
            db.execute("UPDATE runs SET state='interrupted' WHERE state='running'")
            db.execute("UPDATE work SET state='interrupted' WHERE state='pending'")
            db.execute("UPDATE episodes SET lifecycle='interrupted',root_done=1 "
                       "WHERE lifecycle IN ('running','waiting')")
            db.execute("UPDATE jobs SET state='pending' WHERE state='running'")

    def sources(self, episode_id: str, limit: int = 100) -> dict:
        episode = self.episode(episode_id)
        return {
            "episode": episode,
            "events": self.rows("SELECT * FROM events WHERE episode_id=? ORDER BY ordinal LIMIT ?",
                                (episode_id, limit)),
            "evidence": self.rows("SELECT * FROM evidence WHERE episode_id=?", (episode_id,)),
            "relations": self.rows("SELECT * FROM edges WHERE via=?", (episode_id,)),
            "retrievals": self.rows("SELECT * FROM retrievals WHERE episode_id=?", (episode_id,)),
        }
