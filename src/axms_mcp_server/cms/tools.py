from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from axms_mcp_server.cms import TOOL_NAMES
from axms_mcp_server.cms.contract import (
    CmsApplyReady,
    CmsDiscard,
    CmsPreview,
    CmsRevalidation,
    CmsValidation,
)
from axms_mcp_server.cms.preview import (
    NaturalCmsToolError,
    apply_preview,
    create_preview,
    discard_preview,
    resolve_target,
    revalidate_preview,
    validate_command,
)
from axms_mcp_server.common.catalog import validate_registered_tool_names


def register_cms_tools(server: MCPServer[Any]) -> tuple[str, ...]:
    def resolve_cms_target(
        resource: dict[str, Any], currentState: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve one Spring-supplied CMS resource snapshot without database access."""
        return _run(resolve_target, resource, currentState)

    def validate_cms_command(
        resource: dict[str, Any],
        command: dict[str, Any],
        currentState: dict[str, Any],
    ) -> CmsValidation:
        """Validate one structured CMS command against its supplied resource snapshot."""
        return _run(validate_command, resource, command, currentState)

    def create_cms_preview(
        resource: dict[str, Any],
        command: dict[str, Any],
        currentState: dict[str, Any],
    ) -> CmsPreview:
        """Create a deterministic preview; Spring remains the preview store."""
        return _run(create_preview, resource, command, currentState)

    def discard_cms_preview(previewId: str, previewHash: str) -> CmsDiscard:
        """Return a deterministic preview invalidation acknowledgement."""
        return _run(discard_preview, previewId, previewHash)

    def revalidate_cms_preview(
        previewId: str,
        previewHash: str,
        resource: dict[str, Any],
        command: dict[str, Any],
        currentState: dict[str, Any],
    ) -> CmsRevalidation:
        """Compare an approved preview with the latest Spring-supplied resource snapshot."""
        return _run(
            revalidate_preview,
            previewId,
            previewHash,
            resource,
            command,
            currentState,
        )

    def apply_cms_preview(
        previewId: str,
        previewHash: str,
        resource: dict[str, Any],
        command: dict[str, Any],
        currentState: dict[str, Any],
    ) -> CmsApplyReady:
        """Return an apply-ready command; only Spring CmsService may mutate Core DB."""
        return _run(
            apply_preview,
            previewId,
            previewHash,
            resource,
            command,
            currentState,
        )

    registered = (
        ("resolve_cms_target", resolve_cms_target),
        ("validate_cms_command", validate_cms_command),
        ("create_cms_preview", create_cms_preview),
        ("discard_cms_preview", discard_cms_preview),
        ("revalidate_cms_preview", revalidate_cms_preview),
        ("apply_cms_preview", apply_cms_preview),
    )
    registered_names = tuple(name for name, _ in registered)
    validate_registered_tool_names(registered_names)
    if registered_names != TOOL_NAMES:
        raise ValueError("Natural CMS MCP registration must match the approved catalog order.")
    for name, function in registered:
        server.add_tool(function, name=name, structured_output=True)
        _forbid_extra_arguments(server, name)
    return registered_names


def _run(function: Any, *arguments: Any) -> Any:
    try:
        return function(*arguments)
    except NaturalCmsToolError as exception:
        raise ToolError(f"{exception.code}: {exception}") from exception


def _forbid_extra_arguments(server: MCPServer[Any], name: str) -> None:
    tool = server._tool_manager.get_tool(name)
    if tool is None:
        raise ValueError("Registered Natural CMS MCP tool could not be resolved.")
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)
