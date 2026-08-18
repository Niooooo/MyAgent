import unittest

from order_total import calculate_total


class OrderTotalTests(unittest.TestCase):
    def test_sums_every_price(self) -> None:
        self.assertEqual(calculate_total([499, 100, 250]), 849)

    def test_does_not_mutate_the_input(self) -> None:
        prices = [300, 100, 200]
        calculate_total(prices)
        self.assertEqual(prices, [300, 100, 200])

    def test_empty_collection(self) -> None:
        self.assertEqual(calculate_total([]), 0)

    def test_accepts_an_immutable_collection(self) -> None:
        self.assertEqual(calculate_total((125, 75)), 200)


if __name__ == "__main__":
    unittest.main()
