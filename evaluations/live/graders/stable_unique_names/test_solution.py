import unittest

from names import stable_unique_names


class SolutionTests(unittest.TestCase):
    def test_removes_exact_duplicates_in_first_seen_order(self):
        names = ["Ada", "Lin", "Ada", "ada", "Lin"]
        self.assertEqual(stable_unique_names(names), ["Ada", "Lin", "ada"])
        self.assertEqual(names, ["Ada", "Lin", "Ada", "ada", "Lin"])


if __name__ == "__main__":
    unittest.main()
