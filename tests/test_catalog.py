from __future__ import annotations

import unittest

from axms_mcp_server.common.catalog import (
    KNOWN_TOOL_CONTRACTS,
    KNOWN_TOOL_NAMES,
    PRODUCTION_TOOL_NAMES,
    validate_known_catalog,
    validate_registered_tool_names,
)


class ToolCatalogTest(unittest.TestCase):
    def test_known_catalog_is_the_fixed_coding_and_cms_allowlist(self) -> None:
        validate_known_catalog()

        self.assertEqual(13, len(KNOWN_TOOL_CONTRACTS))
        self.assertEqual(
            {
                "read_file",
                "search_code",
                "read_diff",
                "apply_patch",
                "run_check",
                "check_package_allowlist",
                "scan_changed_files",
                "resolve_cms_target",
                "validate_cms_command",
                "create_cms_preview",
                "discard_cms_preview",
                "revalidate_cms_preview",
                "apply_cms_preview",
            },
            set(KNOWN_TOOL_NAMES),
        )

    def test_production_catalog_registers_only_the_approved_coding_tools(self) -> None:
        self.assertEqual(
            (
                "read_file",
                "search_code",
                "read_diff",
                "apply_patch",
                "run_check",
                "check_package_allowlist",
                "scan_changed_files",
            ),
            PRODUCTION_TOOL_NAMES,
        )
        self.assertEqual(
            frozenset(PRODUCTION_TOOL_NAMES),
            validate_registered_tool_names(PRODUCTION_TOOL_NAMES),
        )

    def test_registration_rejects_unknown_or_duplicate_names(self) -> None:
        with self.assertRaises(ValueError):
            validate_registered_tool_names(["not_approved"])
        with self.assertRaises(ValueError):
            validate_registered_tool_names(["read_file", "read_file"])


if __name__ == "__main__":
    unittest.main()
