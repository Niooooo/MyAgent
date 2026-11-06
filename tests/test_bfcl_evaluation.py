"""Scorer, single-agent runtime and report regressions."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from myagent.evaluation.bfcl.experiment import run_case
from myagent.evaluation.bfcl.integration_cases import build_integration_cases
from myagent.evaluation.bfcl.reporting import summarize
from myagent.evaluation.bfcl.runtime import Budget, BudgetEndpoint
from myagent.evaluation.bfcl.schema import DIMENSIONS, load_cases, validate_cases
from myagent.evaluation.bfcl.scoring import normalized, score, state_matches
from myagent.evaluation.bfcl.tool_cases import build_tool_cases


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()

    def case(self, prefix):
        return copy.deepcopy(next(case for case in self.cases if case["id"].startswith(prefix)))

    def test_single_runtime_executes_tool_and_integration_tasks(self):
        for prefix in ("tool_selection_read", "goal_achievement_merge_policy"):
            with self.subTest(prefix=prefix):
                row = run_case(self.case(prefix))
                self.assertTrue(row["passed"], row["failures"])
                self.assertEqual(row["variant"], "single")
                self.assertEqual(len(row["stages"]), 1)
                self.assertTrue(row["stages"][0]["protocol_ok"])

    def test_report_counts_cases_separately_from_attempts_and_breaks_down_failures(self):
        case = self.case("tool_selection_read")
        first = run_case(case, attempt=1)
        second = copy.deepcopy(first)
        second.update(attempt=2, passed=False, failures=["answer"])
        report = summarize([first, second], {"expected_runs": 3, "provider": "replay"})
        dimension = report["dimensions"][case["dimension"]]
        self.assertEqual(dimension["unique_cases"], 1)
        self.assertEqual(dimension["runs"], 2)
        self.assertEqual(dimension["pass_rate"], 0.5)
        self.assertEqual(dimension["failed_checks"], {"answer": 1})
        self.assertFalse(report["complete"])

    def test_cli_resumes_after_interruption_and_keeps_saved_failures(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        from myagent.evaluation.bfcl import __main__ as cli

        case = self.case("tool_selection_read")
        saved = run_case(case, attempt=1)
        saved.update(passed=False, failures=["answer"])
        saved["checks"]["answer"] = False
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(StringIO()):
            arguments = ["run", "--provider", "replay", "--repeat", "2", "--case", case["id"], "--output", temporary]
            with patch.object(cli, "run_case", side_effect=[saved, KeyboardInterrupt()]):
                with self.assertRaises(KeyboardInterrupt):
                    cli.main(arguments)
            output = Path(temporary)
            checkpoint = (output / "runs.jsonl").read_bytes()
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["completed_runs"], 1)
            with patch.object(cli, "run_case", wraps=run_case) as execute:
                self.assertEqual(cli.main([*arguments, "--resume"]), 1)
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(execute.call_args.kwargs["attempt"], 2)
            self.assertTrue((output / "runs.jsonl").read_bytes().startswith(checkpoint))
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["dimensions"][case["dimension"]]["passed"], 1)

    def observed(self, case):
        batches = {identifier: index for index, group in enumerate(case["parallel_groups"], 1) for identifier in group}
        return [{**copy.deepcopy(call), "result": {"ok": call["ok"], "passed": True},
                 "batch": batches.get(call["id"], index + 1)} for index, call in enumerate(case["calls"])]

    def grade(self, case, calls, **overrides):
        arguments = dict(final_text=json.dumps({"status": case["expected_status"], "answer": case["answer"]}),
                         files=case["expected_files"], protocol_ok=True, errors=[])
        arguments.update(overrides)
        return score(case, calls, **arguments)

    def test_exactly_thirty_per_dimension_and_generated_manifest_matches(self):
        self.assertEqual(validate_cases(self.cases)["dimensions"], dict.fromkeys(DIMENSIONS, 30))
        self.assertEqual(self.cases, build_tool_cases() + build_integration_cases())
        self.assertEqual(len(self.cases), 330)

    def test_repetition_and_duplicate_tags_cannot_fill_dimension_quota(self):
        with self.assertRaises(ValueError):
            validate_cases(self.cases[:-1])
        bad = copy.deepcopy(self.cases)
        bad[-1] = bad[0]
        with self.assertRaises(ValueError):
            validate_cases(bad)

    def test_fixture_escape_and_cycle_rejected(self):
        case = self.case("call_order_read_copy")
        for invalid in ("../secret", "C:/secret", "x/../../secret", ".myagent/secret"):
            with self.subTest(invalid=invalid):
                bad = {**case, "fixture": {invalid: "test"}}
                with self.assertRaises(ValueError):
                    validate_cases([bad], complete=False)
        case["edges"] += [["c1", "c0"]]
        with self.assertRaises(ValueError):
            validate_cases([case], complete=False)

    def test_correct_terminal_state_does_not_hide_wrong_argument(self):
        case = self.case("argument_accuracy_read")
        calls = self.observed(case)
        calls[0]["arguments"]["start_line"] = 1
        self.assertFalse(self.grade(case, calls)["passed"])

    def test_json_boolean_does_not_equal_integer(self):
        self.assertNotEqual(normalized("grep", {"case_sensitive": True}), normalized("grep", {"case_sensitive": 1}))
        self.assertFalse(state_matches({"x.json": '{"enabled":1}'}, {"x.json": '{"enabled":true}'}))

    def test_optional_defaults_and_relative_paths_are_equivalent(self):
        self.assertEqual(normalized("read_file", {"path": "./docs/a.txt"}),
                         normalized("read_file", {"path": "docs/a.txt", "start_line": 1, "max_lines": 400}))

    def test_extra_duplicate_and_missing_calls_fail(self):
        case = self.case("tool_restraint_read")
        calls = self.observed(case)
        self.assertTrue(self.grade(case, calls)["passed"])
        self.assertFalse(self.grade(case, calls + calls)["passed"])
        self.assertFalse(self.grade(case, [])["passed"])

    def test_tool_use_on_no_tool_case_fails(self):
        case = self.case("irrelevance_arithmetic")
        self.assertTrue(self.grade(case, [])["passed"])
        calls = self.observed(self.case("tool_selection_read"))
        self.assertFalse(self.grade(case, calls)["passed"])

    def test_dependency_reversed_or_guessed_in_same_response_fails(self):
        case = self.case("call_order_read_copy")
        calls = self.observed(case)
        self.assertTrue(self.grade(case, calls)["passed"])
        calls[0]["batch"], calls[1]["batch"] = 2, 1
        self.assertFalse(self.grade(case, calls)["checks"]["dependency_order"])
        calls[0]["batch"] = calls[1]["batch"] = 1
        self.assertFalse(self.grade(case, calls)["checks"]["dependency_order"])

    def test_independent_calls_permute_but_must_share_batch(self):
        case = self.case("parallel_calls_two_reads")
        calls = self.observed(case)
        self.assertTrue(self.grade(case, calls[::-1])["passed"])
        calls[1]["batch"] = 2
        self.assertFalse(self.grade(case, calls)["checks"]["parallel_batches"])

    def test_error_recovery_requires_failure_then_success(self):
        case = self.case("error_recovery_missing_file")
        calls = self.observed(case)
        self.assertTrue(self.grade(case, calls)["passed"])
        calls[1]["result"]["ok"] = False
        self.assertFalse(self.grade(case, calls)["passed"])

    def test_false_completion_and_protocol_errors_fail(self):
        case = self.case("completion_honesty_failed_validation")
        calls = self.observed(case)
        self.assertFalse(self.grade(case, calls, integration=True,
                                    final_text='{"status":"completed","answer":"done"}')["passed"])
        self.assertFalse(self.grade(case, calls, protocol_ok=False)["passed"])

    def test_claiming_blocked_without_requested_evidence_fails(self):
        case = self.case("completion_honesty_failed_validation")
        self.assertFalse(self.grade(case, [], integration=True)["passed"])

    def test_validation_before_last_write_fails_and_unrelated_mutation_fails(self):
        case = self.case("goal_achievement_merge_policy")
        calls = self.observed(case)
        self.assertTrue(self.grade(case, calls, integration=True)["passed"])
        calls = [calls[-1], *calls[:-1]]
        self.assertFalse(self.grade(case, calls, integration=True)["checks"]["validation_after_last_write"])
        files = {**case["expected_files"], "unrelated.txt": "changed"}
        self.assertFalse(self.grade(case, self.observed(case), files=files, integration=True)["passed"])




    def test_shared_request_budget_and_output_cap(self):
        class Endpoint:
            def create(self, **kwargs):
                self.last = kwargs
                return SimpleNamespace(output=[])
        raw = SimpleNamespace(responses=Endpoint())
        budget = Budget(request_limit=1, output_tokens_per_request=12)
        first = BudgetEndpoint(raw, budget, 2)
        second = BudgetEndpoint(raw, budget, 2)
        first.create(max_output_tokens=500)
        self.assertEqual(raw.responses.last["max_output_tokens"], 12)
        with self.assertRaisesRegex(RuntimeError, "budget_exhausted"):
            second.create()




if __name__ == "__main__":
    unittest.main()
