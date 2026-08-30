from __future__ import annotations

from collections.abc import Iterable

from axms_mcp_server.cms import TOOL_NAMES as CMS_TOOL_NAMES
from axms_mcp_server.coding import TOOL_NAMES as CODING_TOOL_NAMES
from axms_mcp_server.common.contract import ToolContract


KNOWN_TOOL_CONTRACTS = tuple(
    ToolContract(package, name)
    for package, names in (("coding", CODING_TOOL_NAMES), ("cms", CMS_TOOL_NAMES))
    for name in names
)
KNOWN_TOOL_NAMES = frozenset(contract.name for contract in KNOWN_TOOL_CONTRACTS)
PRODUCTION_TOOL_NAMES: tuple[str, ...] = CODING_TOOL_NAMES + CMS_TOOL_NAMES


def validate_known_catalog() -> None:
    names = [contract.name for contract in KNOWN_TOOL_CONTRACTS]
    if len(names) != len(set(names)):
        raise ValueError("Known MCP tool names must be unique.")
    if any(contract.package not in {"coding", "cms"} for contract in KNOWN_TOOL_CONTRACTS):
        raise ValueError("Known MCP tools must belong to coding or cms.")


def validate_registered_tool_names(tool_names: Iterable[str]) -> frozenset[str]:
    names = tuple(tool_names)
    if len(names) != len(set(names)):
        raise ValueError("Registered MCP tool names must be unique.")
    unknown = set(names).difference(KNOWN_TOOL_NAMES)
    if unknown:
        raise ValueError("Registered MCP tool names must be in the approved catalog.")
    return frozenset(names)
