import unittest

from secrets import mask_secret


class SolutionTests(unittest.TestCase):
    def test_masks_all_but_last_four_characters(self):
        self.assertEqual(mask_secret("abcdefgh"), "****efgh")
        self.assertEqual(mask_secret("abcd"), "****")
        self.assertEqual(mask_secret("ab"), "**")
        self.assertEqual(mask_secret(""), "")


if __name__ == "__main__":
    unittest.main()
