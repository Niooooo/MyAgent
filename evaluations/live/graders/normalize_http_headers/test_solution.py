import unittest

from headers import normalize_headers


class SolutionTests(unittest.TestCase):
    def test_normalizes_copy_and_last_duplicate_wins(self):
        source = {" Content-Type ": " json ", "X-ID": " first ", " x-id ": "second"}
        result = normalize_headers(source)
        self.assertEqual(result, {"content-type": "json", "x-id": "second"})
        self.assertEqual(len(source), 3)


if __name__ == "__main__":
    unittest.main()
