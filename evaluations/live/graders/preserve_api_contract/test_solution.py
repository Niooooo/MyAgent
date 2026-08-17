import inspect
import unittest

from calculator import clamp


class ClampTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(clamp(-1), 0)
        self.assertEqual(clamp(50), 50)
        self.assertEqual(clamp(101), 100)

    def test_signature(self):
        self.assertEqual(
            str(inspect.signature(clamp)),
            "(value, minimum=0, maximum=100)",
        )


if __name__ == "__main__":
    unittest.main()
