from __future__ import annotations

from typing import Any, TypedDict


class CmsResource(TypedDict):
    type: str
    id: str


class CmsPreview(TypedDict):
    previewId: str
    previewHash: str
    resource: CmsResource
    command: dict[str, Any]
    before: dict[str, Any]
    after: dict[str, Any]


class CmsValidation(TypedDict):
    valid: bool
    validationHash: str
    resource: CmsResource
    command: dict[str, Any]


class CmsDiscard(TypedDict):
    discarded: bool
    previewId: str
    previewHash: str


class CmsRevalidation(TypedDict):
    valid: bool
    previewId: str
    previewHash: str
    currentPreviewHash: str


class CmsApplyReady(TypedDict):
    applyReady: bool
    previewId: str
    previewHash: str
    resource: CmsResource
    command: dict[str, Any]
