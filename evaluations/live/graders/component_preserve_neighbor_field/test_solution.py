import json
import unittest
from pathlib import Path


class SolutionTests(unittest.TestCase):
    def test_only_timeout_value_changes(self):
        payload = json.loads(Path("config/service.json").read_text(encoding="utf-8"))
        self.assertEqual(
            payload,
            {"endpoint": "/v1/run", "timeout_ms": 1500, "retries": 2},
        )


if __name__ == "__main__":
    unittest.main()
