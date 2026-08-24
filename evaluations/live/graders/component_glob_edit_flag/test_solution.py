import unittest
from pathlib import Path


class SolutionTests(unittest.TestCase):
    def test_switch_is_on(self):
        self.assertEqual(
            Path("flags/current.switch").read_text(encoding="utf-8").strip(),
            "state=on",
        )


if __name__ == "__main__":
    unittest.main()
