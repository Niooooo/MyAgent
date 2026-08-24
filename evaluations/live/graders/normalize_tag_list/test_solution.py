import unittest

from tags import normalize_tags


class SolutionTests(unittest.TestCase):
    def test_trims_and_removes_empty_values_without_mutation(self):
        tags = [" api ", "", "  ", "agent", "api"]
        self.assertEqual(normalize_tags(tags), ["api", "agent", "api"])
        self.assertEqual(tags, [" api ", "", "  ", "agent", "api"])


if __name__ == "__main__":
    unittest.main()
