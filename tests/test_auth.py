from __future__ import annotations

import unittest

from axms_mcp_server.common.auth import validate_service_token


class ServiceTokenValidationTest(unittest.TestCase):
    def test_rejects_blank_or_whitespace_tokens(self) -> None:
        for token in ("", " ", "token with space", "token\nvalue"):
            with self.subTest(token=repr(token)):
                with self.assertRaises(ValueError):
                    validate_service_token(token)

    def test_accepts_opaque_token(self) -> None:
        validate_service_token("a" * 43)


if __name__ == "__main__":
    unittest.main()
