import unittest

import shipping


class SolutionTests(unittest.TestCase):
    def test_uses_shared_rates_and_weight_surcharge(self):
        self.assertEqual(shipping.shipping_cost(1), 5)
        self.assertEqual(shipping.shipping_cost(3), 9)
        self.assertEqual(shipping.shipping_cost(2, express=True), 14)

if __name__ == "__main__":
    unittest.main()
