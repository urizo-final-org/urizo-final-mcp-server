from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from axms_mcp_server.cms.contract import (
    CmsApplyReady,
    CmsDiscard,
    CmsPreview,
    CmsResource,
    CmsRevalidation,
    CmsValidation,
)


RESOURCE_TYPE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class NaturalCmsToolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_target(
    resource: dict[str, Any], current_state: dict[str, Any]
) -> dict[str, Any]:
    normalized_resource, normalized_state = _target(resource, current_state)
    return {
        "resolved": True,
        "resource": normalized_resource,
        "currentState": normalized_state,
        "currentHash": _digest(normalized_state),
    }


def validate_command(
    resource: dict[str, Any],
    command: dict[str, Any],
    current_state: dict[str, Any],
) -> CmsValidation:
    normalized_resource, normalized_state = _target(resource, current_state)
    normalized_command = _command(command)
    return {
        "valid": True,
        "validationHash": _digest(
            {
                "resource": normalized_resource,
                "command": normalized_command,
                "currentState": normalized_state,
            }
        ),
        "resource": normalized_resource,
        "command": normalized_command,
    }


def create_preview(
    resource: dict[str, Any],
    command: dict[str, Any],
    current_state: dict[str, Any],
) -> CmsPreview:
    normalized_resource, normalized_state = _target(resource, current_state)
    normalized_command = _command(command)
    after = dict(normalized_state)
    after.update(normalized_command["fields"])
    subject = {
        "resource": normalized_resource,
        "command": normalized_command,
        "before": normalized_state,
        "after": after,
    }
    preview_hash = _digest(subject)
    return {
        "previewId": str(uuid5(NAMESPACE_URL, "axms:natural-cms:" + preview_hash)),
        "previewHash": preview_hash,
        **subject,
    }


def discard_preview(preview_id: str, preview_hash: str) -> CmsDiscard:
    _preview_reference(preview_id, preview_hash)
    return {
        "discarded": True,
        "previewId": preview_id,
        "previewHash": preview_hash,
    }


def revalidate_preview(
    preview_id: str,
    preview_hash: str,
    resource: dict[str, Any],
    command: dict[str, Any],
    current_state: dict[str, Any],
) -> CmsRevalidation:
    _preview_reference(preview_id, preview_hash)
    current = create_preview(resource, command, current_state)
    valid = (
        current["previewId"] == preview_id
        and current["previewHash"] == preview_hash
    )
    return {
        "valid": valid,
        "previewId": preview_id,
        "previewHash": preview_hash,
        "currentPreviewHash": current["previewHash"],
    }


def apply_preview(
    preview_id: str,
    preview_hash: str,
    resource: dict[str, Any],
    command: dict[str, Any],
    current_state: dict[str, Any],
) -> CmsApplyReady:
    revalidated = revalidate_preview(
        preview_id, preview_hash, resource, command, current_state
    )
    if not revalidated["valid"]:
        raise NaturalCmsToolError(
            "CMS_PREVIEW_STALE", "The Natural CMS preview no longer matches the resource."
        )
    normalized_resource, _ = _target(resource, current_state)
    return {
        "applyReady": True,
        "previewId": preview_id,
        "previewHash": preview_hash,
        "resource": normalized_resource,
        "command": _command(command),
    }


def _target(
    resource: dict[str, Any], current_state: dict[str, Any]
) -> tuple[CmsResource, dict[str, Any]]:
    if not isinstance(resource, dict) or set(resource) != {"type", "id"}:
        raise NaturalCmsToolError("CMS_TARGET_INVALID", "CMS resource is invalid.")
    resource_type = resource.get("type")
    resource_id = resource.get("id")
    if (
        not isinstance(resource_type, str)
        or RESOURCE_TYPE.fullmatch(resource_type) is None
        or not isinstance(resource_id, str)
        or RESOURCE_ID.fullmatch(resource_id) is None
    ):
        raise NaturalCmsToolError("CMS_TARGET_INVALID", "CMS resource is invalid.")
    if not isinstance(current_state, dict) or not current_state:
        raise NaturalCmsToolError("CMS_TARGET_INVALID", "CMS current state is invalid.")
    if "id" in current_state and str(current_state["id"]) != resource_id:
        raise NaturalCmsToolError("CMS_TARGET_INVALID", "CMS resource id does not match.")
    _bounded_json(current_state)
    return {"type": resource_type, "id": resource_id}, dict(current_state)


def _command(command: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(command, dict) or set(command) != {"operation", "fields"}:
        raise NaturalCmsToolError("CMS_COMMAND_INVALID", "CMS command is invalid.")
    operation = command.get("operation")
    fields = command.get("fields")
    if operation != "UPDATE" or not isinstance(fields, dict) or not fields:
        raise NaturalCmsToolError("CMS_COMMAND_INVALID", "CMS command is invalid.")
    if any(
        not isinstance(name, str)
        or not name
        or len(name) > 80
        or not isinstance(value, str)
        or len(value) > 20_000
        for name, value in fields.items()
    ):
        raise NaturalCmsToolError("CMS_COMMAND_INVALID", "CMS command fields are invalid.")
    normalized = {"operation": operation, "fields": dict(fields)}
    _bounded_json(normalized)
    return normalized


def _preview_reference(preview_id: str, preview_hash: str) -> None:
    try:
        UUID(preview_id)
    except (ValueError, TypeError, AttributeError):
        raise NaturalCmsToolError("CMS_PREVIEW_INVALID", "CMS preview id is invalid.") from None
    if not isinstance(preview_hash, str) or SHA256_DIGEST.fullmatch(preview_hash) is None:
        raise NaturalCmsToolError("CMS_PREVIEW_INVALID", "CMS preview hash is invalid.")


def _bounded_json(value: Any) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise NaturalCmsToolError("CMS_CONTRACT_INVALID", "CMS JSON is invalid.") from None
    if len(encoded.encode("utf-8")) > 64_000:
        raise NaturalCmsToolError("CMS_CONTRACT_INVALID", "CMS JSON is too large.")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
