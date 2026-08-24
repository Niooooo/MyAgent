import unittest

from lifecycle_impl import lifecycle_status


class SolutionTests(unittest.TestCase):
    def test_status_is_ready(self):
        self.assertEqual(lifecycle_status(), "ready")


if __name__ == "__main__":
    unittest.main()
