import json
import unittest
from pathlib import Path


class SolutionTests(unittest.TestCase):
    def test_nested_limits_and_preserved_fields(self):
        payload = json.loads(Path("config/limits.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["limits"], {"min": 0, "max": 120, "burst": 150})
        self.assertEqual(payload["service"], "gateway")
        self.assertIs(payload["enabled"], True)


if __name__ == "__main__":
    unittest.main()
