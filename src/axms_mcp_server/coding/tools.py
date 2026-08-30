from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from axms_mcp_server.coding import TOOL_NAMES
from axms_mcp_server.coding.contract import (
    ApplyPatchResult,
    ChangedFilesScanResult,
    PackageAllowlistResult,
    ReadDiffResult,
    ReadFileResult,
    RunCheckResult,
    SearchCodeResult,
)
from axms_mcp_server.coding.workspace import CodingToolError, CodingWorkspace
from axms_mcp_server.common.catalog import validate_registered_tool_names


def register_coding_tools(
    server: MCPServer[Any],
    workspace_root: Path,
    git_executable: Path,
) -> tuple[str, ...]:
    workspace_service = CodingWorkspace(workspace_root, git_executable)

    def read_file(workspace: str, expectedHead: str, path: str) -> ReadFileResult:
        """Read one approved UTF-8 text file from a confined Coding workspace."""
        try:
            return workspace_service.read_file(workspace, expectedHead, path)
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    def search_code(
        workspace: str,
        expectedHead: str,
        query: str,
        scope: str = ".",
    ) -> SearchCodeResult:
        """Search approved UTF-8 source files for a bounded literal string."""
        try:
            return workspace_service.search_code(workspace, expectedHead, query, scope)
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    def read_diff(workspace: str, expectedHead: str) -> ReadDiffResult:
        """Return the complete protected-path and secret-scanned Git approval diff."""
        try:
            return workspace_service.read_diff(workspace, expectedHead)
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    def apply_patch(
        workspace: str,
        expectedHead: str,
        expectedDiffDigest: str,
        patch: str,
    ) -> ApplyPatchResult:
        """Apply one bounded canonical text patch to the Git index and worktree."""
        try:
            return workspace_service.apply_patch(
                workspace,
                expectedHead,
                expectedDiffDigest,
                patch,
            )
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    def run_check(
        workspace: str,
        expectedHead: str,
        expectedDiffDigest: str,
        profile: str,
    ) -> RunCheckResult:
        """Run a registered non-code-executing deterministic check profile."""
        try:
            return workspace_service.run_check(
                workspace,
                expectedHead,
                expectedDiffDigest,
                profile,
            )
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    def check_package_allowlist(
        workspace: str,
        expectedHead: str,
        expectedDiffDigest: str,
    ) -> PackageAllowlistResult:
        """Reject dependency manifest and lock-file changes in the locked MVP policy."""
        try:
            return workspace_service.check_package_allowlist(
                workspace,
                expectedHead,
                expectedDiffDigest,
            )
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    def scan_changed_files(
        workspace: str,
        expectedHead: str,
        expectedDiffDigest: str,
    ) -> ChangedFilesScanResult:
        """Scan added lines for fixed high-signal secret rules without returning values."""
        try:
            return workspace_service.scan_changed_files(
                workspace,
                expectedHead,
                expectedDiffDigest,
            )
        except CodingToolError as exception:
            raise _tool_error(exception) from exception

    registered = (
        ("read_file", read_file),
        ("search_code", search_code),
        ("read_diff", read_diff),
        ("apply_patch", apply_patch),
        ("run_check", run_check),
        ("check_package_allowlist", check_package_allowlist),
        ("scan_changed_files", scan_changed_files),
    )
    registered_names = tuple(name for name, _ in registered)
    validate_registered_tool_names(registered_names)
    if registered_names != TOOL_NAMES:
        raise ValueError("Coding MCP registration must match the approved catalog order.")
    for name, function in registered:
        server.add_tool(function, name=name, structured_output=True)
        _forbid_extra_arguments(server, name)
    return registered_names


def _tool_error(exception: CodingToolError) -> ToolError:
    return ToolError(f"{exception.code}: {exception}")


def _forbid_extra_arguments(server: MCPServer[Any], name: str) -> None:
    # mcp 2.1.1 does not expose per-tool model configuration through add_tool.
    # Tighten the pinned SDK's generated argument model before the server is returned.
    tool = server._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError("Registered Coding MCP tool could not be resolved.")
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)
