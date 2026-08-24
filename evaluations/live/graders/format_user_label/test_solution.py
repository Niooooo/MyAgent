import unittest

from user_label import format_user_label


class SolutionTests(unittest.TestCase):
    def test_formats_and_cleans_names(self):
        self.assertEqual(format_user_label(" Ada ", " lovelace "), "Lovelace, Ada")
        self.assertEqual(format_user_label("Lin", ""), "Lin")
        self.assertEqual(format_user_label("", " Turing "), "Turing")
        self.assertEqual(format_user_label(" ", " "), "")


if __name__ == "__main__":
    unittest.main()
