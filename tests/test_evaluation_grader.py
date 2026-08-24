import tempfile
import unittest
from pathlib import Path

from myagent.evaluation.grader import run_hidden_tests


class EvaluationGraderTests(unittest.TestCase):
    def test_hidden_grader_uses_a_deterministic_hash_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            grader = root / "grader"
            workspace.mkdir()
            grader.mkdir()
            (grader / "test_seed.py").write_text(
                "import os\n"
                "import unittest\n\n"
                "class SeedTests(unittest.TestCase):\n"
                "    def test_hash_seed_is_fixed(self):\n"
                "        self.assertEqual(os.environ.get('PYTHONHASHSEED'), '0')\n",
                encoding="utf-8",
            )

            result = run_hidden_tests(workspace, grader, timeout_seconds=5)

        self.assertTrue(result.passed, result.stderr)


if __name__ == "__main__":
    unittest.main()
