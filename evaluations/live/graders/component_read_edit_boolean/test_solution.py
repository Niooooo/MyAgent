import unittest

from feature import enabled


class SolutionTests(unittest.TestCase):
    def test_enabled_returns_true(self):
        self.assertIs(enabled(), True)


if __name__ == "__main__":
    unittest.main()
