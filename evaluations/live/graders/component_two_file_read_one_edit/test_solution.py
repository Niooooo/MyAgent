import unittest

from rate import RATE


class SolutionTests(unittest.TestCase):
    def test_rate_matches_source_config(self):
        self.assertEqual(RATE, 0.15)


if __name__ == "__main__":
    unittest.main()
