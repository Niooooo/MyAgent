import unittest

from toggles import parse_toggle


class SolutionTests(unittest.TestCase):
    def test_supported_true_values(self):
        for value in (True, 1, "true", " TRUE ", "1", "on", "yes"):
            with self.subTest(value=value):
                self.assertIs(parse_toggle(value), True)

    def test_supported_false_and_invalid_values(self):
        for value in (False, 0, "false", " 0 ", "off", "no", "unknown", None, 2):
            with self.subTest(value=value):
                self.assertIs(parse_toggle(value), False)


if __name__ == "__main__":
    unittest.main()
