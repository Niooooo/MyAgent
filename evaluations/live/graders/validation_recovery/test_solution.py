import unittest

from retry import parse_retry_count


class RetryTests(unittest.TestCase):
    def test_valid_values(self):
        for value in (0, 3, 5, "0", "4", "5"):
            self.assertEqual(parse_retry_count(value), int(value))

    def test_invalid_values(self):
        for value in (-1, 6, "6", "bad", None, 2.5):
            self.assertEqual(parse_retry_count(value), 0)


if __name__ == "__main__":
    unittest.main()
