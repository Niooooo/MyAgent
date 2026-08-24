import unittest

from invoice import invoice_total
from money import to_cents


class SolutionTests(unittest.TestCase):
    def test_half_up_cent_conversion(self):
        self.assertEqual(to_cents("1.005"), 101)
        self.assertEqual(to_cents("2.674"), 267)

    def test_invoice_reuses_cent_conversion(self):
        self.assertEqual(invoice_total(["1.005", "2.674", "0.10"]), 378)
        self.assertEqual(invoice_total([]), 0)


if __name__ == "__main__":
    unittest.main()
