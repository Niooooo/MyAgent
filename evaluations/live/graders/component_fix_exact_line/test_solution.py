import unittest

from message import MESSAGE


class SolutionTests(unittest.TestCase):
    def test_message_is_corrected(self):
        self.assertEqual(MESSAGE, "status ready")


if __name__ == "__main__":
    unittest.main()
