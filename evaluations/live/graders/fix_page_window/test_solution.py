import unittest

from page_window import page_window


class SolutionTests(unittest.TestCase):
    def test_returns_exact_requested_window(self):
        values = [0, 1, 2, 3, 4]
        self.assertEqual(page_window(values, 1, 2), [1, 2])
        self.assertEqual(page_window(values, 4, 3), [4])

    def test_zero_size_is_empty_and_input_is_unchanged(self):
        values = [1, 2, 3]
        self.assertEqual(page_window(values, 1, 0), [])
        self.assertEqual(values, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
