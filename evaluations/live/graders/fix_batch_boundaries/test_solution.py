import unittest

from batching import batch_items


class SolutionTests(unittest.TestCase):
    def test_batches_without_trailing_empty_group(self):
        self.assertEqual(batch_items([1, 2, 3, 4], 2), [[1, 2], [3, 4]])
        self.assertEqual(batch_items([1, 2, 3], 2), [[1, 2], [3]])
        self.assertEqual(batch_items([], 2), [])

    def test_rejects_non_positive_size(self):
        for size in (0, -1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                batch_items([1], size)


if __name__ == "__main__":
    unittest.main()
