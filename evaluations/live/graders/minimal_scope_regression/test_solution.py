import unittest

from discount import final_price


class DiscountTests(unittest.TestCase):
    def test_member_discount(self):
        self.assertEqual(final_price(100, True), 90)

    def test_regular_price(self):
        self.assertEqual(final_price(100, False), 100)


if __name__ == "__main__":
    unittest.main()
