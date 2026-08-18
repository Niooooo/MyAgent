import unittest

from status_impl import current_status


class StatusTests(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(current_status(), "ready")


if __name__ == "__main__":
    unittest.main()
