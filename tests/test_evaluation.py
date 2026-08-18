import contextlib
import io
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from myagent.agent import DEFAULT_MODEL
from myagent.evaluation.cases import DatasetError, load_cases
from myagent.evaluation.cli import main
from myagent.evaluation.runner import _dataset_sha256, run_case, write_reports
from myagent.evaluation.trace import ReplayClient, aggregate_usage, extract_usage


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = REPOSITORY_ROOT / "evaluations" / "live" / "cases.jsonl"


class EvaluationDatasetTests(unittest.TestCase):
    def test_pilot_dataset_has_four_distinct_repository_tasks(self) -> None:
        cases = load_cases(DATASET_PATH)

        self.assertEqual(
            [case.id for case in cases],
            [
                "fix_order_total",
                "normalize_slug",
                "repair_retry_config",
                "cross_file_greeting",
            ],
        )
        self.assertEqual(
            {case.category for case in cases},
            {"bugfix", "feature", "configuration"},
        )

    def test_manifest_paths_cannot_escape_the_dataset_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "schema_version": 1,
                "id": "escape_case",
                "category": "bugfix",
                "description": "invalid fixture path",
                "prompt": "do work",
                "fixture": "../outside",
                "grader": "graders/escape",
                "replay_script": "replays/escape.json",
                "max_tool_rounds": 2,
                "grader_timeout_seconds": 5,
                "allowed_approvals": [],
                "expected_changed_files": ["src/example.py"],
            }
            manifest = root / "cases.jsonl"
            manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(DatasetError, "escapes the dataset"):
                load_cases(manifest)

    def test_validate_command_does_not_require_code_execution_consent(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["validate", "--dataset", str(DATASET_PATH)])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["cases"], 4)

    def test_run_command_requires_explicit_code_execution_consent(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "run",
                    "--dataset",
                    str(DATASET_PATH),
                    "--case",
                    "fix_order_total",
                ]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("--allow-code-execution", stderr.getvalue())


class EvaluationRunnerTests(unittest.TestCase):
    def test_replay_runs_full_fixture_grader_trace_and_report_pipeline(self) -> None:
        case = load_cases(DATASET_PATH)[0]
        result = run_case(
            case,
            ReplayClient(case.replay_responses),
            provider="replay",
            model=DEFAULT_MODEL,
        )

        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["baseline_grader"]["passed"])
        self.assertTrue(result["final_grader"]["passed"])
        self.assertEqual(result["workspace_diff"]["changed_files"], ["src/order_total.py"])
        self.assertTrue(result["trace"]["call_id_sequence_matches"])
        self.assertEqual(result["metrics"]["call_output_coverage"], 1.0)
        self.assertIsNone(result["metrics"]["token_usage"])
        self.assertIsNone(result["metrics"]["cost_usd"])

        with tempfile.TemporaryDirectory() as temporary:
            summary = write_reports(
                temporary,
                [result],
                provider="replay",
                model=DEFAULT_MODEL,
                dataset_path=DATASET_PATH,
            )
            output = Path(temporary)
            self.assertTrue((output / "runs.jsonl").is_file())
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "summary.md").is_file())
            self.assertTrue(summary["gate_passed"])
            self.assertFalse(summary["live_model_executed"])

    def test_denied_write_is_traced_and_cannot_change_the_fixture(self) -> None:
        original = load_cases(DATASET_PATH)[0]
        case = replace(original, allowed_approvals=frozenset())

        result = run_case(
            case,
            ReplayClient(case.replay_responses),
            provider="replay",
            model=DEFAULT_MODEL,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["workspace_diff"]["changed_files"], [])
        write_output = next(
            item
            for item in result["trace"]["function_call_outputs"]
            if item["call_id"] == "write_order_total"
        )
        self.assertEqual(write_output["result"]["code"], "permission_denied")
        self.assertFalse(result["trace"]["approvals"][0]["approved"])
        approval_assertion = next(
            item for item in result["assertions"] if item["name"] == "approval_policy"
        )
        self.assertFalse(approval_assertion["passed"])

    def test_validation_must_succeed_after_the_last_mutation(self) -> None:
        original = load_cases(DATASET_PATH)[0]
        responses = (
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "test_before_fix",
                        "name": "run_acceptance_tests",
                        "arguments": {},
                    }
                ],
                "output_text": "",
            },
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "write_after_test",
                        "name": "write_file",
                        "arguments": {
                            "path": "src/order_total.py",
                            "content": (
                                "def calculate_total(prices_cents):\n"
                                "    return sum(prices_cents)\n"
                            ),
                        },
                    }
                ],
                "output_text": "",
            },
            {"output": [], "output_text": "Fixed after the only test run."},
        )
        case = replace(original, replay_responses=responses)

        result = run_case(
            case,
            ReplayClient(case.replay_responses),
            provider="replay",
            model=DEFAULT_MODEL,
        )

        self.assertTrue(result["final_grader"]["passed"])
        validation_assertion = next(
            item
            for item in result["assertions"]
            if item["name"] == "validation_after_changes"
        )
        self.assertFalse(validation_assertion["passed"])
        self.assertEqual(result["status"], "failed")

    def test_dataset_hash_covers_fixture_and_grader_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "live"
            shutil.copytree(DATASET_PATH.parent, copied)
            copied_manifest = copied / "cases.jsonl"
            initial = _dataset_sha256(copied_manifest)
            fixture = copied / "fixtures" / "fix_order_total" / "README.md"
            fixture.write_text(
                fixture.read_text(encoding="utf-8") + "\nchanged\n",
                encoding="utf-8",
            )

            self.assertNotEqual(_dataset_sha256(copied_manifest), initial)

    def test_usage_normalization_keeps_unavailable_fields_null(self) -> None:
        self.assertIsNone(extract_usage(None))
        self.assertEqual(
            extract_usage({"input_tokens": 12, "output_tokens": 3}),
            {"input_tokens": 12, "output_tokens": 3, "total_tokens": None},
        )
        self.assertEqual(
            aggregate_usage(
                [
                    {
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "total_tokens": 15,
                        }
                    },
                    {
                        "usage": {
                            "input_tokens": 8,
                            "output_tokens": None,
                            "total_tokens": None,
                        }
                    },
                ]
            ),
            {
                "input_tokens": 20,
                "output_tokens": None,
                "total_tokens": None,
                "partial": True,
            },
        )


if __name__ == "__main__":
    unittest.main()
