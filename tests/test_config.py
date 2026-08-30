from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from axms_mcp_server.config import Settings


class SettingsTest(unittest.TestCase):
    def test_reads_explicit_environment_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory, "service-token")
            token_file.write_text("a" * 43 + "\n", encoding="utf-8")
            settings = Settings.from_environment(
                {
                    "AXMS_MCP_HOST": "127.0.0.1",
                    "AXMS_MCP_PORT": "18091",
                    "AXMS_MCP_SERVICE_TOKEN_FILE": str(token_file),
                    "AXMS_MCP_ALLOWED_HOSTS": "localhost:*,127.0.0.1:*",
                    "AXMS_MCP_ALLOWED_ORIGINS": "http://localhost:*",
                    "AXMS_MCP_MAX_REQUEST_BODY_SIZE": "4096",
                    "AXMS_MCP_LOG_LEVEL": "warning",
                }
            )

            self.assertEqual("127.0.0.1", settings.host)
            self.assertEqual(18091, settings.port)
            self.assertEqual(("localhost:*", "127.0.0.1:*"), settings.allowed_hosts)
            self.assertEqual(("http://localhost:*",), settings.allowed_origins)
            self.assertEqual(4096, settings.max_request_body_size)
            self.assertEqual("a" * 43, settings.read_service_token())

    def test_rejects_unsafe_or_invalid_settings(self) -> None:
        invalid_environments = (
            {"AXMS_MCP_PORT": "0"},
            {"AXMS_MCP_PORT": "not-a-number"},
            {"AXMS_MCP_ALLOWED_HOSTS": ""},
            {"AXMS_MCP_MAX_REQUEST_BODY_SIZE": "0"},
            {"AXMS_MCP_LOG_LEVEL": "trace"},
        )
        for environment in invalid_environments:
            with self.subTest(environment=environment):
                with self.assertRaises(ValueError):
                    Settings.from_environment(environment)


if __name__ == "__main__":
    unittest.main()
