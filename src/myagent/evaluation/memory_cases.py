"""Fixed held-out policies; generated assets never enter a previous task snapshot."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path


def add_heldout_cases(cases: dict, directory: Path) -> None:
    base = cases["repair_retry_config"]
    for name, attempts, jitter, level in (
        ("retry_budget", 5, False, "warn"),
        ("retry_policy_revision", 2, False, "error"),
        ("retry_deadline", 4, True, "warn"),
    ):
        fixture = directory / name / "fixture"
        grader = directory / name / "grader"
        (fixture / "config").mkdir(parents=True)
        grader.mkdir(parents=True)
        content = {"service": {"endpoint": "https://api.example.test",
                               "retry": {"attempts": "3", "backoff_ms": 500}},
                   "logging": {"level": "debug"}}
        (fixture / "config/service.json").write_text(json.dumps(content, indent=2), encoding="utf-8")
        (fixture / "README.md").write_text(
            f"Service policy: retry attempts must be integer {attempts}; jitter must be "
            f"{str(jitter).lower()}; logging level must be {level}. "
            "Preserve endpoint and backoff_ms. This policy replaces earlier deployment settings.\n",
            encoding="utf-8",
        )
        expected = json.loads(json.dumps(content))
        expected["service"]["retry"].update(attempts=attempts, jitter=jitter)
        expected["logging"]["level"] = level
        grader.joinpath("test_policy.py").write_text(
            "import json\nimport unittest\nfrom pathlib import Path\n\n"
            "class PolicyTest(unittest.TestCase):\n"
            "    def test_policy(self):\n"
            "        actual = json.loads(Path('config/service.json').read_text(encoding='utf-8'))\n"
            f"        self.assertEqual(actual, {expected!r})\n"
            "        self.assertIs(type(actual['service']['retry']['attempts']), int)\n",
            encoding="utf-8",
        )
        replay = (
            {"output": [{"type": "function_call", "call_id": "policy", "name": "read_file",
                         "arguments": {"path": "README.md"}}], "output_text": ""},
            {"output": [{"type": "function_call", "call_id": "write", "name": "write_file",
                         "arguments": {"path": "config/service.json", "content": json.dumps(expected, indent=2)}}],
             "output_text": ""},
            {"output": [{"type": "function_call", "call_id": "check", "name": "run_acceptance_tests",
                         "arguments": {}}], "output_text": ""},
            {"output": [], "output_text": "Applied the current deployment policy and checked it."},
        )
        cases[name] = replace(
            base, id=name, fixture_dir=fixture, grader_dir=grader, replay_responses=replay,
            prompt="Read README.md and repair config/service.json for the current deployment policy. "
                   "Preserve unrelated fields and run acceptance tests.",
        )
