import unittest

from healthy import health


class SolutionTests(unittest.TestCase):
    def test_health_is_already_green(self):
        self.assertEqual(health(), "green")


if __name__ == "__main__":
    unittest.main()
