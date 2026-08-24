import unittest
from pathlib import Path


class SolutionTests(unittest.TestCase):
    def test_marker_has_exact_content(self):
        self.assertEqual(
            Path("output/READY.marker").read_text(encoding="utf-8"),
            "READY\n",
        )


if __name__ == "__main__":
    unittest.main()
