import json
import unittest
from pathlib import Path


class SolutionTests(unittest.TestCase):
    def test_logging_values_and_unrelated_fields(self):
        payload = json.loads(Path("config/logging.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["logging"]["level"], "warning")
        self.assertIs(payload["logging"]["json"], True)
        self.assertEqual(payload["logging"]["path"], "logs/myagent.log")
        self.assertEqual(payload["service"], "worker")


if __name__ == "__main__":
    unittest.main()
