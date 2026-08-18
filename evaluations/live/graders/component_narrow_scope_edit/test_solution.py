import unittest

from target import is_enabled


class TargetTests(unittest.TestCase):
    def test_enabled(self):
        self.assertIs(is_enabled(), True)


if __name__ == "__main__":
    unittest.main()
