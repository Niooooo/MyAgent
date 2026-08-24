import unittest

from ports import parse_port


class SolutionTests(unittest.TestCase):
    def test_accepts_valid_integer_and_decimal_string(self):
        self.assertEqual(parse_port(443), 443)
        self.assertEqual(parse_port(" 8081 "), 8081)

    def test_invalid_values_use_default(self):
        for value in (True, 0, 65536, "1.5", "", None):
            with self.subTest(value=value):
                self.assertEqual(parse_port(value), 8080)
        self.assertEqual(parse_port("bad", default=9000), 9000)


if __name__ == "__main__":
    unittest.main()
