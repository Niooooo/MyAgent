import unittest

from greeting import greeting
from name_utils import normalize_display_name


class GreetingTests(unittest.TestCase):
    def test_normalizes_display_name(self) -> None:
        self.assertEqual(
            normalize_display_name("  ada   lovelace  "),
            "Ada Lovelace",
        )

    def test_collapses_tabs_and_newlines(self) -> None:
        self.assertEqual(normalize_display_name("grace\thopper"), "Grace Hopper")

    def test_greeting_uses_normalized_name(self) -> None:
        self.assertEqual(greeting("  alan   turing "), "Hello, Alan Turing!")


if __name__ == "__main__":
    unittest.main()
