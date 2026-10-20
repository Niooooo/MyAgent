"""Three-layer graph operations, revision history and disposable projections."""

from __future__ import annotations

import hashlib
import json
import time

from .episodes import EpisodeStore
from .storage import encode


class MemoryStore(EpisodeStore):
    def insight(self, insight_id: str) -> dict:
        rows = self.rows("SELECT * FROM insights WHERE id=?", (insight_id,))
        if not rows:
            raise ValueError("unknown insight")
        item = rows[0]
        item["roles"] = json.loads(item["roles"])
        item["sources"] = self.rows("SELECT * FROM insight_sources WHERE insight_id=?", (insight_id,))
        return item

    def learn(self, episode_id: str, version: int, proposals: list[dict], *, finish_job: bool = True) -> list[str]:
        episode = self.episode(episode_id)
        if episode["version"] != version or episode["outcome"] == "unknown":
            raise ValueError("learning needs current verified evidence")
        if episode["lifecycle"] not in {"ended", "interrupted"}:
            raise ValueError("learning must wait for related work")
        events = {r["id"] for r in self.rows("SELECT id FROM events WHERE episode_id=?", (episode_id,))}
        verification = {r["event_id"] for r in self.rows(
            "SELECT event_id FROM evidence WHERE episode_id=? AND active=1", (episode_id,))}
        validated = []
        for proposal in proposals[:4]:
            text, conditions, roles, refs = (proposal.get(key) for key in ("text", "conditions", "roles", "event_ids"))
            if not isinstance(text, str) or not text.strip() or len(text) > 1600:
                raise ValueError("insight requires bounded actionable text")
            if not isinstance(conditions, str) or not conditions.strip() or len(conditions) > 1000:
                raise ValueError("insight requires applicability conditions")
            if not isinstance(roles, list) or not roles or any(not isinstance(r, str) or not r.strip() for r in roles):
                raise ValueError("insight requires applicable roles")
            if not isinstance(refs, list) or not refs or not set(refs) <= events or not set(refs) & verification:
                raise ValueError("insight must cite source events including validation evidence")
            fingerprint = encode([text.strip(), conditions.strip(), sorted(roles)])
            iid = "i_" + hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
            validated.append((iid, text.strip(), conditions.strip(), roles, refs))
        learned = []
        with self.transaction() as db:
            current = db.execute("SELECT version,outcome,lifecycle FROM episodes WHERE id=?", (episode_id,)).fetchone()
            if current is None or current[0] != version or current[1] == "unknown":
                raise ValueError("episode changed during extraction")
            if db.execute("SELECT 1 FROM blocked_learning WHERE episode_id=? AND version=?",
                          (episode_id, version)).fetchone():
                return []
            if db.execute("SELECT 1 FROM work WHERE episode_id=? AND state='pending'", (episode_id,)).fetchone():
                raise ValueError("background work still pending")
            for iid, text, conditions, roles, refs in validated:
                if db.execute("SELECT 1 FROM tombstones WHERE id=?", (iid,)).fetchone():
                    continue
                existing = db.execute("SELECT status,version FROM insights WHERE id=?", (iid,)).fetchone()
                if existing and existing[0] != "active":
                    continue
                db.execute("INSERT OR IGNORE INTO insights(id,text,conditions,roles) VALUES(?,?,?,?)",
                           (iid, text, conditions, encode(roles)))
                added = db.execute("INSERT OR IGNORE INTO insight_sources VALUES(?,?,?,?,?)",
                                   (iid, episode_id, encode(refs), episode["outcome"], version)).rowcount
                insight_version = existing["version"] if existing else 1
                if existing and added:
                    insight_version += 1
                    db.execute("UPDATE insights SET version=? WHERE id=?", (insight_version, iid))
                self._edge(db, episode_id, iid, "supports", episode_id, encode(refs))
                sources = [dict(row) for row in db.execute(
                    "SELECT * FROM insight_sources WHERE insight_id=?", (iid,)
                )]
                db.execute("INSERT OR IGNORE INTO insight_versions VALUES(?,?,?)",
                           (iid, insight_version, encode({"text": text, "conditions": conditions,
                                                        "roles": roles, "sources": sources})))
                learned.append(iid)
            used = db.execute("SELECT DISTINCT insight_id FROM usage WHERE episode_id=?", (episode_id,)).fetchall()
            for old in used:
                for new in learned:
                    if old[0] != new:
                        self._edge(db, old[0], new, "co_used", episode_id, "used and learned in this episode")
            if finish_job:
                db.execute("UPDATE jobs SET state='done',error='' WHERE episode_id=? AND version=?", (episode_id, version))
        return learned

    def change_insight(self, insight_id: str, action: str, *, reason: str,
                       evidence_id: str | None = None, text: str | None = None) -> None:
        item = self.insight(insight_id)
        if action not in {"disable", "delete", "revise"} or not reason.strip():
            raise ValueError("action and correction reason are required")
        if action == "revise" and (not isinstance(text, str) or not text.strip() or len(text) > 1600):
            raise ValueError("revision requires bounded text")
        evidence = self.rows("SELECT * FROM evidence WHERE id=?", (evidence_id,)) if evidence_id else []
        if evidence_id and not evidence:
            raise ValueError("unknown counterexample evidence")
        with self.transaction() as db:
            current = db.execute("SELECT version FROM insights WHERE id=?", (insight_id,)).fetchone()
            if current is None or current["version"] != item["version"]:
                raise ValueError("insight changed while its correction was being prepared")
            for source in item["sources"]:
                episode = db.execute("SELECT version FROM episodes WHERE id=?", (source["episode_id"],)).fetchone()
                if episode:
                    db.execute("INSERT OR IGNORE INTO blocked_learning VALUES(?,?)",
                               (source["episode_id"], episode[0]))
                    db.execute("UPDATE jobs SET state='obsolete' WHERE episode_id=? AND version=?",
                               (source["episode_id"], episode[0]))
            if evidence:
                e = evidence[0]
                source_version = db.execute("SELECT version FROM episodes WHERE id=?", (e["episode_id"],)).fetchone()[0]
                db.execute("INSERT OR REPLACE INTO insight_sources VALUES(?,?,?,?,?)",
                           (insight_id, e["episode_id"], encode([e["event_id"]]),
                            "support" if e["passed"] else "counterexample", source_version))
                db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,?,?)",
                           (e["episode_id"], insight_id, "supported" if e["passed"] else "counterexample", evidence_id))
            if action == "delete":
                db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (insight_id,))
                db.execute("DELETE FROM insights WHERE id=?", (insight_id,))
                db.execute("DELETE FROM edges WHERE source=? OR target=?", (insight_id, insight_id))
            else:
                changed = dict(item, text=text.strip() if text else item["text"],
                               status="disabled" if action == "disable" else "active",
                               version=item["version"] + 1, reason=reason)
                db.execute("UPDATE insights SET text=?,status=?,version=? WHERE id=?",
                           (changed["text"], changed["status"], changed["version"], insight_id))
                db.execute("INSERT INTO insight_versions VALUES(?,?,?)",
                           (insight_id, changed["version"], encode(changed)))
                # Original content must not be resurrected by a pending extraction.
                db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (insight_id,))
            db.execute("DELETE FROM retrievals WHERE instr(body,?)>0", (insight_id,))
        self.rebuild_projections()

    def apply_insight_feedback(self, episode_id: str, updates: list[dict]) -> None:
        """Only explicitly cited lessons and criterion-specific evidence may change."""
        if self.episode(episode_id)["outcome"] == "unknown":
            raise ValueError("unknown outcomes cannot validate historical lessons")
        validated = []
        for update in updates[:4]:
            iid, evidence_id = update.get("insight_id"), update.get("evidence_id")
            action, reason = update.get("action"), update.get("reason")
            if action not in {"support", "revise", "disable"} or not isinstance(reason, str) or not reason.strip():
                raise ValueError("insight feedback requires an action and an evidence-based reason")
            if not self.rows("SELECT 1 FROM usage WHERE episode_id=? AND insight_id=? AND stage='cited'",
                             (episode_id, iid)):
                raise ValueError("injection alone cannot validate or refute an insight")
            evidence = self.rows("SELECT * FROM evidence WHERE id=? AND episode_id=? AND active=1",
                                 (evidence_id, episode_id))
            if not evidence or bool(evidence[0]["passed"]) != (action == "support"):
                raise ValueError("feedback must cite matching support or counterexample evidence")
            self.insight(iid)
            text = update.get("text")
            if action == "revise" and (not isinstance(text, str) or not text.strip() or len(text) > 1600):
                raise ValueError("revised insight must contain bounded text")
            validated.append((iid, evidence_id, action, reason, text))
        for iid, evidence_id, action, reason, text in validated:
            # Job retries must not append repeated revisions for the same evidence.
            stage = "supported" if action == "support" else "counterexample"
            if self.rows("SELECT 1 FROM usage WHERE episode_id=? AND insight_id=? AND stage=? AND evidence_id=?",
                         (episode_id, iid, stage, evidence_id)):
                continue
            if action == "support":
                self.use(episode_id, iid, stage, evidence_id)
            else:
                self.change_insight(iid, action, reason=reason, evidence_id=evidence_id, text=text)
            self.edge(episode_id, iid, stage, episode_id, encode({"evidence_id": evidence_id, "reason": reason}))

    def use(self, episode_id: str, insight_id: str, stage: str, evidence_id: str = "") -> None:
        if stage not in {"retrieved", "provided", "cited", "supported", "counterexample"}:
            raise ValueError("invalid memory usage stage")
        self.insight(insight_id)
        if stage in {"supported", "counterexample"}:
            evidence = self.rows("SELECT passed FROM evidence WHERE id=? AND episode_id=? AND active=1",
                                 (evidence_id, episode_id))
            if not evidence or bool(evidence[0]["passed"]) != (stage == "supported"):
                raise ValueError("support requires evidence from this task")
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO usage VALUES(?,?,?,?)",
                       (episode_id, insight_id, stage, evidence_id))

    def delete_episode(self, episode_id: str) -> None:
        self.episode(episode_id)
        with self.transaction() as db:
            nodes = [row[0] for row in db.execute("SELECT id FROM events WHERE episode_id=?", (episode_id,))]
            nodes += [row[0] for row in db.execute("SELECT id FROM runs WHERE episode_id=?", (episode_id,))]
            nodes.append(episode_id)
            db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (episode_id,))
            db.execute("DELETE FROM episodes WHERE id=?", (episode_id,))
            for node in nodes:
                db.execute("DELETE FROM edges WHERE source=? OR target=? OR via=?", (node, node, node))
                db.execute("DELETE FROM retrievals WHERE instr(body,?)>0", (node,))
            orphaned = db.execute("SELECT id FROM insights WHERE id NOT IN (SELECT insight_id FROM insight_sources)").fetchall()
            for row in orphaned:
                db.execute("INSERT OR IGNORE INTO tombstones VALUES(?)", (row[0],))
                db.execute("DELETE FROM insights WHERE id=?", (row[0],))
                db.execute("DELETE FROM edges WHERE source=? OR target=?", (row[0], row[0]))
                db.execute("DELETE FROM retrievals WHERE instr(body,?)>0", (row[0],))
        self.rebuild_projections()
        live = {row["blob"] for row in self.rows("SELECT DISTINCT blob FROM events WHERE blob IS NOT NULL")}
        for path in self.path("blobs").glob("*.txt"):
            name = "blobs/" + path.name
            if name not in live:
                self.path(name).unlink(missing_ok=True)

    def projection_record(self, record_id: str) -> dict:
        if record_id.startswith("q_"):
            item = self.episode(record_id)
            return {"id": record_id, "version": item["version"], "episode_id": record_id,
                    "text": f'{item["task"]}\nOutcome: {item["outcome"]}\nCriteria: {encode(item["criteria"])}'}
        if record_id.startswith("i_"):
            item = self.insight(record_id)
            if item["status"] != "active" or not item["sources"]:
                raise ValueError("inactive insight")
            return {"id": record_id, "version": item["version"], "episode_id": item["sources"][0]["episode_id"],
                    "text": f'{item["conditions"]}\nRoles: {encode(item["roles"])}\n{item["text"]}'}
        rows = self.rows("SELECT * FROM events WHERE id=?", (record_id,))
        if not rows:
            raise ValueError("unknown projection record")
        item = rows[0]
        return {"id": record_id, "version": 1, "episode_id": item["episode_id"],
                "text": f'Role: {item["role"]}\nKind: {item["kind"]}\n{item["body"]}'}

    def rebuild_projections(self, *, deadline: float | None = None, max_records: int | None = None) -> None:
        with self._lock:
            sql = ("SELECT id FROM episodes UNION ALL SELECT id FROM insights WHERE status='active' "
                   "UNION ALL SELECT id FROM events WHERE kind NOT IN ('memory','request_succeeded')")
            records = self.rows(sql + " LIMIT ?", (max_records + 1,)) if max_records else self.rows(sql)
            if max_records and len(records) > max_records:
                raise TimeoutError("projection rebuild budget exhausted; run the rebuild command")
            live = set()
            for row in records:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("projection rebuild timed out")
                record = self.projection_record(row["id"])
                name = f'projections/{record["id"]}.txt'
                live.add(name)
                self.atomic_text(name, encode({key: record[key] for key in ("id", "version", "episode_id")}) + "\n" + record["text"])
            for path in self.path("projections").glob("*.txt"):
                name = "projections/" + path.name
                if name not in live:
                    self.path(name).unlink(missing_ok=True)
            self.atomic_text("projection-version", str(self.version))

    def ensure_projections(self, *, deadline: float | None = None) -> None:
        path = self.path("projection-version")
        if not path.exists() or path.read_text(encoding="utf-8") != str(self.version):
            self.rebuild_projections(deadline=deadline, max_records=5000 if deadline else None)
