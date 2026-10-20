"""Model-initiated memory edits remain ordinary permission-guarded tools."""

from ..tooling import FunctionTool

COLLABORATIVE_TOOL_NAMES = frozenset({
    "inspect_collaborative_memory", "change_collaborative_insight", "cite_collaborative_insight",
})


def memory_tools(service):
    if service is None or service.store is None:
        return []
    store = service.store

    def inspect_collaborative_memory(episode_id: str):
        return {"ok": True, **store.sources(episode_id)}

    def change_collaborative_insight(insight_id: str, action: str, reason: str,
                                     evidence_id: str | None = None, text: str | None = None):
        store.change_insight(insight_id, action, reason=reason, evidence_id=evidence_id, text=text)
        return {"ok": True, "id": insight_id, "action": action}

    def cite_collaborative_insight(insight_id: str):
        scope = service._context.get()
        if scope is None:
            return {"ok": False, "error": "no active task"}
        store.use(scope.episode_id, insight_id, "cited")
        return {"ok": True, "id": insight_id, "episode_id": scope.episode_id}

    return [
        FunctionTool("inspect_collaborative_memory", "Read a historical task and its observable source evidence.",
                     {"type": "object", "properties": {"episode_id": {"type": "string"}},
                      "required": ["episode_id"], "additionalProperties": False}, inspect_collaborative_memory),
        FunctionTool("change_collaborative_insight", "Disable, delete or revise an erroneous historical insight.",
                     {"type": "object", "properties": {
                         "insight_id": {"type": "string"}, "action": {"type": "string", "enum": ["disable", "delete", "revise"]},
                         "reason": {"type": "string"}, "evidence_id": {"type": ["string", "null"]},
                         "text": {"type": ["string", "null"]}},
                      "required": ["insight_id", "action", "reason"], "additionalProperties": False},
                     change_collaborative_insight),
        FunctionTool("cite_collaborative_insight", "Record that this task explicitly used a supplied insight; this is not proof of correctness.",
                     {"type": "object", "properties": {"insight_id": {"type": "string"}},
                      "required": ["insight_id"], "additionalProperties": False}, cite_collaborative_insight),
    ]
