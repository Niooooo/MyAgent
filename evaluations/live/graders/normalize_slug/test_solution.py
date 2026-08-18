import unittest

from slug import slugify


class SlugTests(unittest.TestCase):
    def test_collapses_whitespace_and_punctuation(self) -> None:
        self.assertEqual(slugify("  Hello,   World!  "), "hello-world")

    def test_treats_underscores_and_hyphens_as_one_separator(self) -> None:
        self.assertEqual(slugify("API_v2---Ready"), "api-v2-ready")

    def test_strips_leading_and_trailing_separators(self) -> None:
        self.assertEqual(slugify("***Already Clean***"), "already-clean")

    def test_returns_empty_for_no_ascii_alphanumerics(self) -> None:
        self.assertEqual(slugify("***"), "")


if __name__ == "__main__":
    unittest.main()
