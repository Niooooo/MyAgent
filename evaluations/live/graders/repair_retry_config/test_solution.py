import json
import unittest
from pathlib import Path


class ServiceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(
            (Path.cwd() / "config" / "service.json").read_text(encoding="utf-8")
        )

    def test_preserves_endpoint_and_backoff(self) -> None:
        self.assertEqual(
            self.payload["service"]["endpoint"],
            "https://api.example.test",
        )
        self.assertEqual(self.payload["service"]["retry"]["backoff_ms"], 500)

    def test_retry_attempts_is_an_integer(self) -> None:
        attempts = self.payload["service"]["retry"]["attempts"]
        self.assertIs(type(attempts), int)
        self.assertEqual(attempts, 3)

    def test_retry_jitter_is_enabled(self) -> None:
        self.assertIs(self.payload["service"]["retry"]["jitter"], True)

    def test_logging_level_is_info(self) -> None:
        self.assertEqual(self.payload["logging"]["level"], "info")


if __name__ == "__main__":
    unittest.main()
