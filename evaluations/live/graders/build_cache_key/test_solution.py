import unittest

from cache_key import build_cache_key


class SolutionTests(unittest.TestCase):
    def test_normalizes_parts_without_mutating_input(self):
        parts = [" User ", "", " 42 "]
        self.assertEqual(build_cache_key(" Profile ", parts), "profile:user:42")
        self.assertEqual(parts, [" User ", "", " 42 "])


if __name__ == "__main__":
    unittest.main()
