import json
import unittest
from pathlib import Path


class SolutionTests(unittest.TestCase):
    def test_retry_policy_and_preserved_fields(self):
        payload = json.loads(Path("config/retry.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["attempts"], 4)
        self.assertIs(type(payload["attempts"]), int)
        self.assertIs(payload["jitter"], True)
        self.assertEqual(payload["endpoint"], "https://service.invalid/v1")
        self.assertEqual(payload["backoff_ms"], 250)


if __name__ == "__main__":
    unittest.main()
