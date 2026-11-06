"""Reference tool transcripts for deterministic evaluation checks."""

import json

from ..trace import ReplayClient


def script(case):
    calls = case["calls"]
    grouped = {identifier: group for group in case["parallel_groups"] for identifier in group}
    used, responses = set(), []
    for call in calls:
        if call["id"] in used:
            continue
        group = grouped.get(call["id"], [call["id"]])
        batch = [item for item in calls if item["id"] in group]
        used.update(group)
        responses.append({"output": [
            {"type": "function_call", "call_id": item["id"],
             "name": item["name"], "arguments": item["arguments"]} for item in batch]})
    responses.append({"output": [], "output_text": json.dumps(
        {"status": case["expected_status"], "answer": case["answer"]}, ensure_ascii=False)})
    return tuple(responses)


def replay_factory(case):
    return ReplayClient(script(case))
