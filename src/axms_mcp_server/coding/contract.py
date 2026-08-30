from __future__ import annotations

from typing import Literal, TypedDict


class SearchMatch(TypedDict):
    path: str
    line: int
    column: int
    preview: str


class ReadFileResult(TypedDict):
    path: str
    mediaType: str
    sizeBytes: int
    digest: str
    content: str


class SearchCodeResult(TypedDict):
    query: str
    scope: str
    matches: list[SearchMatch]
    truncated: bool


class ReadDiffResult(TypedDict):
    baseSha: str
    changedPaths: list[str]
    sizeBytes: int
    digest: str
    diff: str


class ApplyPatchResult(TypedDict):
    baseSha: str
    changedPaths: list[str]
    diffSizeBytes: int
    diffDigest: str


class RunCheckResult(TypedDict):
    profile: str
    status: Literal["PASSED", "FAILED"]
    durationMs: int
    details: str
    detailsDigest: str


class PackageViolation(TypedDict):
    path: str
    rule: Literal["DEPENDENCY_CHANGE_NOT_ALLOWED"]


class PackageAllowlistResult(TypedDict):
    passed: bool
    changedManifests: list[str]
    violations: list[PackageViolation]
    diffDigest: str


class SecretFinding(TypedDict):
    path: str
    line: int
    rule: str


class ChangedFilesScanResult(TypedDict):
    passed: bool
    changedPaths: list[str]
    findings: list[SecretFinding]
    diffDigest: str
