import unittest

from percentage import clamp_percentage


class SolutionTests(unittest.TestCase):
    def test_clamps_to_closed_percentage_range(self):
        self.assertEqual(clamp_percentage(-3), 0)
        self.assertEqual(clamp_percentage(0), 0)
        self.assertEqual(clamp_percentage(42), 42)
        self.assertEqual(clamp_percentage(100), 100)
        self.assertEqual(clamp_percentage(120), 100)


if __name__ == "__main__":
    unittest.main()
