"""Deterministic memory protocol driver, never evidence of model capability."""

import json
import re
from types import SimpleNamespace

from ..collaborative_memory.storage import encode


class MemoryReplayClient:
    def __init__(self):
        self.responses = self

    def create(self, **request):
        instructions = request["instructions"]
        history = request["input"]
        if instructions.startswith("Extract"):
            source = json.loads(history[0]["content"])
            refs = [event["id"] for event in source["events"]]
            refs += [evidence["event_id"] for evidence in source["evidence"]]
            task = source["episode"]["task"]
            return SimpleNamespace(output=[], output_text=encode({"insights": [{
                "text": "Read the current requirements and run acceptance checks before reusing historical configuration values.",
                "conditions": task[:500], "roles": ["implementer", "reviewer"],
                "event_ids": list(dict.fromkeys(refs)),
            }]}))
        results = [item for item in history if isinstance(item, dict) and item.get("type") == "function_call_output"]
        if not results:
            task = json.loads(history[0]["content"])["task"]
            tokens = re.findall(r"[\w./-]{3,}", task)
            terms = [token for token in tokens if "/" in token or "." in token][:3]
            if not terms:
                terms = tokens[:3] or [task[:50]]
            call = SimpleNamespace(type="function_call", name="collaborative_grep",
                                   call_id="memory-search", arguments=encode({"terms": terms}))
            return SimpleNamespace(output=[call], output_text="")
        hits = [hit for item in results for hit in json.loads(item["output"]).get("hits", [])]
        seeds = list(dict.fromkeys(hit["episode_id"] for hit in hits))[:4]
        return SimpleNamespace(output=[], output_text=encode({
            "seeds": [{"episode_id": seed, "reason": "replay literal artifact match"} for seed in seeds],
        }))
