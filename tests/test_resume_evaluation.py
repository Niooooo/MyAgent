import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from myagent.evaluation.cases import DatasetError, load_cases
from myagent.evaluation.deepseek import DeepSeekResponsesClient
from myagent.evaluation.judge import (
    RUBRIC_VERSION,
    aggregate_verdict_files,
    aggregate_verdicts,
    build_judge_packet,
)
from myagent.evaluation.runner import run_case
from myagent.evaluation.trace import ReplayClient


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESUME_DATASET = REPOSITORY_ROOT / "evaluations" / "live" / "resume-cases.jsonl"


def _chat_response(
    *,
    response_id,
    content="",
    reasoning_content=None,
    tool_calls=(),
    finish_reason="stop",
):
    return SimpleNamespace(
        id=response_id,
        model="deepseek-v4-flash",
        system_fingerprint="fp-test",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=list(tool_calls),
                ),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=4,
            total_tokens=14,
        ),
    )


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class FakeChatCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError("Unexpected chat completion request")
        return self.responses.pop(0)


class ResumeDatasetTests(unittest.TestCase):
    def test_resume_dataset_has_eight_cases_per_track(self):
        cases = load_cases(RESUME_DATASET)

        self.assertEqual(len(cases), 16)
        self.assertEqual(
            sum(case.track == "component" for case in cases),
            8,
        )
        self.assertEqual(
            sum(case.track == "integration" for case in cases),
            8,
        )
        self.assertEqual(
            next(case for case in cases if case.id == "component_no_tool_answer")
            .expected_changed_files,
            (),
        )

    def test_component_without_grader_runs_through_real_agent_loop(self):
        case = next(
            case
            for case in load_cases(RESUME_DATASET)
            if case.id == "component_no_tool_answer"
        )
        client = ReplayClient(
            ({"output": [], "output_text": "READY"},)
        )

        result = run_case(
            case,
            client,
            provider="replay",
            model="fake-model",
        )

        self.assertEqual(result["status"], "passed")
        self.assertIsNone(result["baseline_grader"])
        self.assertIsNone(result["final_grader"])
        self.assertEqual(result["workspace_diff"]["changed_files"], [])


class DeepSeekAdapterTests(unittest.TestCase):
    def test_tool_call_round_trip_preserves_reasoning_and_usage(self):
        endpoint = FakeChatCompletions(
            [
                _chat_response(
                    response_id="response-1",
                    reasoning_content="Need the file.",
                    tool_calls=[
                        _tool_call(
                            "call-1",
                            "read_file",
                            '{"path":"README.md"}',
                        )
                    ],
                    finish_reason="tool_calls",
                ),
                _chat_response(response_id="response-2", content="Complete."),
            ]
        )
        client = DeepSeekResponsesClient(endpoint, thinking="enabled")
        tools = [
            {
                "type": "function",
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
                "strict": True,
            }
        ]
        first = client.responses.create(
            model="deepseek-v4-flash",
            instructions="Use tools.",
            tools=tools,
            input=[{"role": "user", "content": "Read it"}],
            max_output_tokens=100,
        )
        second = client.responses.create(
            model="deepseek-v4-flash",
            instructions="Use tools.",
            tools=tools,
            input=[
                {"role": "user", "content": "Read it"},
                *first.output,
                {
                    "type": "function_call_output",
                    "call_id": "call-1",
                    "output": '{"ok":true}',
                },
            ],
            max_output_tokens=100,
        )

        self.assertEqual(first.output[1].call_id, "call-1")
        self.assertEqual(first.usage.input_tokens, 10)
        self.assertEqual(second.output_text, "Complete.")
        messages = endpoint.requests[1]["messages"]
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertEqual(messages[2]["content"], "")
        self.assertEqual(messages[2]["reasoning_content"], "Need the file.")
        self.assertEqual(messages[3]["role"], "tool")
        self.assertEqual(messages[3]["tool_call_id"], "call-1")
        self.assertNotIn("strict", endpoint.requests[0]["tools"][0]["function"])

    def test_multiple_calls_are_returned_and_client_state_is_isolated(self):
        endpoint = FakeChatCompletions(
            [
                _chat_response(
                    response_id="multi",
                    tool_calls=[
                        _tool_call("a", "glob", '{"pattern":"*.py"}'),
                        _tool_call("b", "read_file", '{"path":"a.py"}'),
                    ],
                    finish_reason="tool_calls",
                )
            ]
        )
        client = DeepSeekResponsesClient(endpoint)

        response = client.responses.create(
            model="deepseek-v4-flash",
            instructions="Work.",
            tools=[],
            input=[{"role": "user", "content": "Find"}],
        )

        self.assertEqual(
            [item.call_id for item in response.output if item.type == "function_call"],
            ["a", "b"],
        )
        isolated = DeepSeekResponsesClient(FakeChatCompletions([]))
        with self.assertRaisesRegex(RuntimeError, "Unknown DeepSeek continuation"):
            isolated.responses.create(
                model="deepseek-v4-flash",
                previous_response_id="multi",
                input=[],
            )


class JudgeAggregationTests(unittest.TestCase):
    def _component_result(self):
        case = next(
            case
            for case in load_cases(RESUME_DATASET)
            if case.id == "component_no_tool_answer"
        )
        return run_case(
            case,
            ReplayClient(({"output": [], "output_text": "READY"},)),
            provider="replay",
            model="fake-model",
        )

    def test_pass_at_k_and_pass_power_k_use_all_attempts(self):
        result = self._component_result()
        packets = []
        verdicts = []
        for attempt in range(1, 4):
            selected = copy.deepcopy(result)
            selected["attempt"] = attempt
            packet = build_judge_packet(
                selected,
                rubric_sha256="rubric",
                judge_prompt_sha256="prompt",
            )
            packets.append(packet)
            passed = attempt != 2
            verdicts.append(
                {
                    "packet_id": packet["packet_id"],
                    "rubric_version": RUBRIC_VERSION,
                    "scores": {
                        "required_tool_coverage": 5,
                        "order_correctness": None,
                        "tool_restraint": 5 if passed else 3,
                    },
                    "label": "success" if passed else "goal_not_met",
                    "premature_completion": False,
                    "hard_failures": [],
                    "evidence": [],
                    "rationale": "The trace follows the packet evidence.",
                    "confidence": 0.9,
                }
            )

        summary, merged = aggregate_verdicts(packets, verdicts)

        case_summary = summary["by_case"]["component_no_tool_answer"]
        self.assertEqual(len(merged), 3)
        self.assertTrue(case_summary["pass_at_k"])
        self.assertFalse(case_summary["pass_power_k"])
        self.assertEqual(summary["component"]["unnecessary_tool_rate"], 0.3333)

    def test_constraint_breach_precedes_generic_failure(self):
        result = self._component_result()
        packet = build_judge_packet(
            result,
            rubric_sha256="rubric",
            judge_prompt_sha256="prompt",
        )
        packet["track"] = "integration"
        verdict = {
            "packet_id": packet["packet_id"],
            "rubric_version": RUBRIC_VERSION,
            "scores": {
                "goal_achievement": 5,
                "constraint_compliance": 2,
                "completion_honesty": 5,
            },
            "label": "goal_met_constraint_breach",
            "premature_completion": False,
            "hard_failures": ["constraint violation"],
            "evidence": [],
            "rationale": "The goal passed but an explicit constraint did not.",
            "confidence": 0.95,
        }

        summary, _ = aggregate_verdicts([packet], [verdict])

        self.assertEqual(summary["labels"]["goal_met_constraint_breach"], 1)

    def test_missing_verdict_is_rejected(self):
        result = self._component_result()
        packet = build_judge_packet(
            result,
            rubric_sha256="rubric",
            judge_prompt_sha256="prompt",
        )

        with self.assertRaisesRegex(DatasetError, "coverage mismatch"):
            aggregate_verdicts([packet], [])

    def test_file_aggregation_writes_machine_and_human_reports(self):
        result = self._component_result()
        packet = build_judge_packet(
            result,
            rubric_sha256="rubric",
            judge_prompt_sha256="prompt",
        )
        verdict = {
            "packet_id": packet["packet_id"],
            "rubric_version": RUBRIC_VERSION,
            "scores": {
                "required_tool_coverage": 5,
                "order_correctness": None,
                "tool_restraint": 5,
            },
            "label": "success",
            "premature_completion": False,
            "hard_failures": [],
            "evidence": [],
            "rationale": "The no-tool trace exactly follows the task.",
            "confidence": 0.99,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packets = root / "packets.jsonl"
            verdicts = root / "verdicts.jsonl"
            output = root / "final"
            packets.write_text(
                json.dumps(packet, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            verdicts.write_text(
                json.dumps(verdict, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            summary = aggregate_verdict_files(packets, verdicts, output)

            self.assertEqual(summary["passed"], 1)
            self.assertTrue((output / "judged-runs.jsonl").is_file())
            self.assertTrue((output / "judge-summary.json").is_file())
            self.assertTrue((output / "judge-summary.md").is_file())


if __name__ == "__main__":
    unittest.main()
