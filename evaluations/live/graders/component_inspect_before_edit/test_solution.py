import unittest

from message import greeting


class MessageTests(unittest.TestCase):
    def test_greeting(self):
        self.assertEqual(greeting(), "hello")


if __name__ == "__main__":
    unittest.main()
