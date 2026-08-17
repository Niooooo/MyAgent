import unittest

from health import health_status


class HealthTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(health_status(), "ok")


if __name__ == "__main__":
    unittest.main()
