import unittest

from metadata import normalized_metadata


class SolutionTests(unittest.TestCase):
    def test_removes_debug_without_mutating_input(self):
        source = {"owner": "agent", "debug": True, "count": 2}
        result = normalized_metadata(source)
        self.assertEqual(result, {"owner": "agent", "count": 2})
        self.assertEqual(source, {"owner": "agent", "debug": True, "count": 2})
        self.assertIsNot(result, source)


if __name__ == "__main__":
    unittest.main()
