from __future__ import annotations

import ast
import hashlib
import hmac
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import threading
import time

from axms_mcp_server.coding.contract import (
    ApplyPatchResult,
    ChangedFilesScanResult,
    PackageAllowlistResult,
    ReadDiffResult,
    ReadFileResult,
    RunCheckResult,
    SearchCodeResult,
    SecretFinding,
)


MAX_TEXT_BYTES = 48 * 1024
MAX_SEARCH_FILE_BYTES = 256 * 1024
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_FILES = 2_000
MAX_CHANGED_FILES = 500
MAX_CHECK_DETAILS_BYTES = 32 * 1024
MAX_GIT_OUTPUT_BYTES = 1024 * 1024
GIT_TIMEOUT_SECONDS = 15

WORKSPACE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
INDEX_MODE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+ ([0-7]{6})$")
CAMEL_WORD = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+"
)
RISKY_GIT_CONFIG = (
    r"^(filter\..*\.(clean|smudge|process|required)|diff\..*\.(command|textconv))$"
)

PROTECTED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".local",
        ".ssh",
        "auth",
        "authorization",
        "authz",
        "authentication",
        "credentials",
        "credential",
        "flyway",
        "migration",
        "migrations",
        "secret",
        "secrets",
        "security",
    }
)
PROTECTED_SECRET_SUFFIXES = frozenset(
    {".jks", ".key", ".keystore", ".p12", ".pem", ".pfx"}
)
PROTECTED_GIT_FILE_NAMES = frozenset({".gitattributes", ".gitmodules"})
PROTECTED_CODE_TOKENS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "authz",
        "credential",
        "credentials",
        "oauth",
        "secret",
        "secrets",
        "security",
    }
)
PROTECTED_CODE_MORPHOLOGY = re.compile(
    r"auth(?!or)|authoriz|authorit|authz|oauth|credentials?|security"
)
DEPENDENCY_FILE_NAMES = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "composer.json",
        "composer.lock",
        "go.mod",
        "go.sum",
        "gradle.properties",
        "package-lock.json",
        "package.json",
        "pnpm-lock.yaml",
        "pom.xml",
        "pyproject.toml",
        "requirements.txt",
        "settings.gradle",
        "settings.gradle.kts",
        "uv.lock",
        "yarn.lock",
    }
)

SECRET_PATTERNS = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "SECRET_ASSIGNMENT",
        re.compile(
            r"(?i)\b(?:api[_-]?key|password|secret|token)\b\s*[:=]\s*"
            r"(?:['\"])?[A-Za-z0-9_./+:-]{12,}"
        ),
    ),
)


class CodingToolError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CodingWorkspace:
    def __init__(self, workspace_root: Path, git_executable: Path) -> None:
        self._configured_root = workspace_root
        self._git_executable = _validated_git_executable(git_executable)
        self._workspace_locks_guard = threading.Lock()
        self._workspace_locks: dict[str, threading.Lock] = {}

    def read_file(self, workspace: str, expected_head: str, path: str) -> ReadFileResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            return self._read_file(root, expected_head, path)

    def _read_file(self, root: Path, expected_head: str, path: str) -> ReadFileResult:
        self._verify_head(root, expected_head)
        target, canonical_path = self._target(root, path, must_exist=True)
        if not target.is_file():
            raise CodingToolError("PATH_POLICY_DENIED", "The requested path is not a regular file.")
        content_bytes = self._read_bytes(target, MAX_TEXT_BYTES)
        content = self._decode_text(content_bytes)
        if self._secret_findings(content, canonical_path):
            raise CodingToolError("SECRET_CONTENT_DENIED", "The requested file contains protected content.")
        return {
            "path": canonical_path,
            "mediaType": "text/plain; charset=utf-8",
            "sizeBytes": len(content_bytes),
            "digest": _digest(content_bytes),
            "content": content,
        }

    def search_code(
        self,
        workspace: str,
        expected_head: str,
        query: str,
        scope: str = ".",
    ) -> SearchCodeResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            return self._search_code(root, expected_head, query, scope)

    def _search_code(
        self,
        root: Path,
        expected_head: str,
        query: str,
        scope: str,
    ) -> SearchCodeResult:
        self._verify_head(root, expected_head)
        if not isinstance(query, str) or not 1 <= len(query) <= 256 or _has_control(query):
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The literal search query is invalid.")
        target, canonical_scope = self._target(
            root,
            scope,
            must_exist=True,
            allow_root=True,
        )
        if target.is_file():
            candidates = [target]
            truncated = False
        else:
            candidates, truncated = self._files_below(root, target)
        matches: list[dict[str, object]] = []
        for candidate in candidates:
            relative = candidate.relative_to(root).as_posix()
            if _is_protected_path(relative) or candidate.is_symlink():
                continue
            try:
                if not candidate.is_file() or candidate.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                raw = candidate.read_bytes()
                text = self._decode_text(raw)
            except (CodingToolError, OSError):
                continue
            if self._secret_findings(text, relative):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                start = 0
                while True:
                    index = line.find(query, start)
                    if index < 0:
                        break
                    if len(matches) == MAX_SEARCH_MATCHES:
                        truncated = True
                        break
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "column": index + 1,
                            "preview": line[:300],
                        }
                    )
                    start = index + max(1, len(query))
                if truncated:
                    break
            if truncated:
                break
        return {
            "query": query,
            "scope": canonical_scope,
            "matches": matches,  # type: ignore[typeddict-item]
            "truncated": truncated,
        }

    def read_diff(self, workspace: str, expected_head: str) -> ReadDiffResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            return self._read_diff(root, expected_head)

    def _read_diff(self, root: Path, expected_head: str) -> ReadDiffResult:
        base_sha = self._verify_head(root, expected_head)
        diff_bytes, changed_paths = self._safe_diff(root)
        return {
            "baseSha": base_sha,
            "changedPaths": changed_paths,
            "sizeBytes": len(diff_bytes),
            "digest": _digest(diff_bytes),
            "diff": self._decode_text(diff_bytes),
        }

    def apply_patch(
        self,
        workspace: str,
        expected_head: str,
        expected_diff_digest: str,
        patch: str,
    ) -> ApplyPatchResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            base_sha = self._verify_head(root, expected_head)
            current_diff = self._diff_bytes(root)
            self._verify_diff_digest(current_diff, expected_diff_digest)
            self._validate_diff(root, current_diff)
            patch_bytes, patch_paths = self._validate_patch(root, patch)
            if len(current_diff) + len(patch_bytes) > MAX_TEXT_BYTES:
                raise CodingToolError(
                    "RESULT_TOO_LARGE",
                    "The resulting approval diff would be too large.",
                )
            self._ensure_index_matches_worktree(root)
            self._ensure_index_paths_regular(root, patch_paths)
            diff_bytes, changed_paths = self._preflight_patch(root, patch_bytes)
            self._run_git(
                root,
                "apply",
                "--index",
                "--whitespace=error-all",
                "-",
                input_bytes=patch_bytes,
            )
        return {
            "baseSha": base_sha,
            "changedPaths": changed_paths,
            "diffSizeBytes": len(diff_bytes),
            "diffDigest": _digest(diff_bytes),
        }

    def run_check(
        self,
        workspace: str,
        expected_head: str,
        expected_diff_digest: str,
        profile: str,
    ) -> RunCheckResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            return self._run_check(root, expected_head, expected_diff_digest, profile)

    def _run_check(
        self,
        root: Path,
        expected_head: str,
        expected_diff_digest: str,
        profile: str,
    ) -> RunCheckResult:
        self._verify_head(root, expected_head)
        diff_bytes = self._diff_bytes(root)
        self._verify_diff_digest(diff_bytes, expected_diff_digest)
        changed_paths = self._changed_paths(root)
        self._ensure_changed_paths_safe(changed_paths)
        self._ensure_diff_modes_regular(diff_bytes)
        self._ensure_index_paths_regular(root, changed_paths)
        started = time.monotonic()
        if profile == "git-diff-check":
            result = self._run_git(
                root,
                "diff",
                "--check",
                "--text",
                "HEAD",
                "--",
                allowed_return_codes=frozenset({0, 1, 2}),
            )
            passed = result.returncode == 0
            details = self._safe_check_details(result.stdout + result.stderr)
        elif profile == "python-syntax":
            errors: list[str] = []
            for changed_path in changed_paths:
                if not changed_path.endswith(".py"):
                    continue
                target, _ = self._target(root, changed_path, must_exist=False)
                if not target.exists():
                    continue
                try:
                    source = self._decode_text(self._read_bytes(target, MAX_SEARCH_FILE_BYTES))
                    ast.parse(source, filename=changed_path)
                except (CodingToolError, OSError, SyntaxError) as exception:
                    if isinstance(exception, SyntaxError):
                        errors.append(
                            f"{changed_path}:{exception.lineno or 0}:{exception.offset or 0}: syntax error"
                        )
                    else:
                        errors.append(f"{changed_path}: syntax check unavailable")
            passed = not errors
            details = "\n".join(errors)
        else:
            raise CodingToolError("CHECK_PROFILE_NOT_ALLOWED", "The requested check profile is not registered.")
        details_bytes = details.encode("utf-8")
        if len(details_bytes) > MAX_CHECK_DETAILS_BYTES:
            details_bytes = details_bytes[:MAX_CHECK_DETAILS_BYTES]
            details = details_bytes.decode("utf-8", errors="ignore") + "\n[output capped]"
            details_bytes = details.encode("utf-8")
        return {
            "profile": profile,
            "status": "PASSED" if passed else "FAILED",
            "durationMs": max(0, int((time.monotonic() - started) * 1000)),
            "details": details,
            "detailsDigest": _digest(details_bytes),
        }

    def check_package_allowlist(
        self,
        workspace: str,
        expected_head: str,
        expected_diff_digest: str,
    ) -> PackageAllowlistResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            return self._check_package_allowlist(
                root,
                expected_head,
                expected_diff_digest,
            )

    def _check_package_allowlist(
        self,
        root: Path,
        expected_head: str,
        expected_diff_digest: str,
    ) -> PackageAllowlistResult:
        self._verify_head(root, expected_head)
        diff_bytes = self._diff_bytes(root)
        self._verify_diff_digest(diff_bytes, expected_diff_digest)
        changed_paths = self._changed_paths(root)
        self._ensure_changed_paths_safe(changed_paths)
        self._ensure_diff_modes_regular(diff_bytes)
        self._ensure_index_paths_regular(root, changed_paths)
        changed_manifests = sorted(path for path in changed_paths if _is_dependency_file(path))
        return {
            "passed": not changed_manifests,
            "changedManifests": changed_manifests,
            "violations": [
                {"path": path, "rule": "DEPENDENCY_CHANGE_NOT_ALLOWED"}
                for path in changed_manifests
            ],
            "diffDigest": _digest(diff_bytes),
        }

    def scan_changed_files(
        self,
        workspace: str,
        expected_head: str,
        expected_diff_digest: str,
    ) -> ChangedFilesScanResult:
        root = self._workspace(workspace)
        with self._workspace_lock(root):
            return self._scan_changed_files(
                root,
                expected_head,
                expected_diff_digest,
            )

    def _scan_changed_files(
        self,
        root: Path,
        expected_head: str,
        expected_diff_digest: str,
    ) -> ChangedFilesScanResult:
        self._verify_head(root, expected_head)
        diff_bytes = self._diff_bytes(root)
        self._verify_diff_digest(diff_bytes, expected_diff_digest)
        changed_paths = self._changed_paths(root)
        self._ensure_changed_paths_safe(changed_paths)
        self._ensure_diff_modes_regular(diff_bytes)
        self._ensure_index_paths_regular(root, changed_paths)
        findings = self._added_findings_for_diff(diff_bytes)
        return {
            "passed": not findings,
            "changedPaths": changed_paths,
            "findings": findings,
            "diffDigest": _digest(diff_bytes),
        }

    def _root(self) -> Path:
        try:
            if self._configured_root.is_symlink() or _is_junction(self._configured_root):
                raise CodingToolError("WORKSPACE_ROOT_INVALID", "The workspace root cannot be a link.")
            root = self._configured_root.resolve(strict=True)
        except FileNotFoundError as exception:
            raise CodingToolError("WORKSPACE_ROOT_UNAVAILABLE", "The workspace root is unavailable.") from exception
        if not root.is_dir():
            raise CodingToolError("WORKSPACE_ROOT_INVALID", "The workspace root is not a directory.")
        return root

    def _workspace(self, workspace: str) -> Path:
        if not isinstance(workspace, str) or not WORKSPACE_KEY.fullmatch(workspace):
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The workspace key is invalid.")
        root = self._root()
        candidate = root / workspace
        try:
            if candidate.is_symlink() or _is_junction(candidate):
                raise CodingToolError("WORKSPACE_SCOPE_DENIED", "The workspace cannot be a link.")
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exception:
            raise CodingToolError("WORKSPACE_NOT_FOUND", "The workspace does not exist.") from exception
        if resolved.parent != root or not resolved.is_dir():
            raise CodingToolError("WORKSPACE_SCOPE_DENIED", "The workspace is outside the configured root.")
        result = self._run_git(resolved, "rev-parse", "--show-toplevel")
        try:
            top_level = Path(self._decode_text(result.stdout).strip()).resolve(strict=True)
        except (OSError, CodingToolError) as exception:
            raise CodingToolError("REPOSITORY_SCOPE_DENIED", "The workspace is not a usable Git worktree.") from exception
        if top_level != resolved:
            raise CodingToolError("REPOSITORY_SCOPE_DENIED", "The Git root does not match the workspace.")
        self._ensure_repository_config_safe(resolved)
        return resolved

    def _target(
        self,
        root: Path,
        wire_path: str,
        *,
        must_exist: bool,
        allow_root: bool = False,
    ) -> tuple[Path, str]:
        parts, canonical = _validate_wire_path(wire_path, allow_root=allow_root)
        if canonical != "." and _is_protected_path(canonical):
            raise CodingToolError("PATH_POLICY_DENIED", "The requested path is protected.")
        target = root.joinpath(*parts)
        cursor = root
        for part in parts:
            cursor = cursor / part
            if cursor.exists() or cursor.is_symlink() or _is_junction(cursor):
                if cursor.is_symlink() or _is_junction(cursor):
                    raise CodingToolError("PATH_POLICY_DENIED", "Linked paths are not allowed.")
        try:
            resolved = target.resolve(strict=must_exist)
        except FileNotFoundError as exception:
            raise CodingToolError("PATH_NOT_FOUND", "The requested path does not exist.") from exception
        if not resolved.is_relative_to(root):
            raise CodingToolError("PATH_POLICY_DENIED", "The requested path leaves the workspace.")
        return resolved, canonical

    def _verify_head(self, root: Path, expected_head: str) -> str:
        if not isinstance(expected_head, str) or not GIT_OBJECT_ID.fullmatch(expected_head):
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The expected Git head is invalid.")
        result = self._run_git(root, "rev-parse", "--verify", "HEAD")
        actual = self._decode_text(result.stdout).strip()
        if not hmac.compare_digest(actual, expected_head):
            raise CodingToolError("CANDIDATE_SHA_MISMATCH", "The workspace Git head changed.")
        return actual

    def _verify_diff_digest(self, diff_bytes: bytes, expected: str) -> None:
        if not isinstance(expected, str) or not SHA256_DIGEST.fullmatch(expected):
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The expected diff digest is invalid.")
        if not hmac.compare_digest(_digest(diff_bytes), expected):
            raise CodingToolError("CONTEXT_DIGEST_MISMATCH", "The workspace diff changed.")

    def _diff_bytes(
        self,
        root: Path,
        *,
        cached: bool = False,
        index_file: Path | None = None,
    ) -> bytes:
        arguments = ["diff"]
        if cached:
            arguments.append("--cached")
        arguments.extend(
            (
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--text",
                "HEAD",
                "--",
            )
        )
        result = self._run_git(
            root,
            *arguments,
            index_file=index_file,
        )
        return result.stdout

    def _changed_paths(
        self,
        root: Path,
        *,
        cached: bool = False,
        index_file: Path | None = None,
    ) -> list[str]:
        arguments = ["diff"]
        if cached:
            arguments.append("--cached")
        arguments.extend(("--name-only", "-z", "HEAD", "--"))
        result = self._run_git(root, *arguments, index_file=index_file)
        paths = [self._decode_text(value) for value in result.stdout.split(b"\0") if value]
        if len(paths) > MAX_CHANGED_FILES:
            raise CodingToolError("RESULT_TOO_LARGE", "The approval diff changes too many files.")
        for path in paths:
            _validate_wire_path(path, allow_root=False)
        return sorted(paths)

    def _safe_diff(self, root: Path) -> tuple[bytes, list[str]]:
        diff_bytes = self._diff_bytes(root)
        changed_paths = self._validate_diff(root, diff_bytes)
        return diff_bytes, changed_paths

    def _validate_diff(
        self,
        root: Path,
        diff_bytes: bytes,
        *,
        cached: bool = False,
        index_file: Path | None = None,
    ) -> list[str]:
        if len(diff_bytes) > MAX_TEXT_BYTES:
            raise CodingToolError("RESULT_TOO_LARGE", "The approval diff is too large.")
        changed_paths = self._changed_paths(
            root,
            cached=cached,
            index_file=index_file,
        )
        self._ensure_changed_paths_safe(changed_paths)
        self._ensure_diff_modes_regular(diff_bytes)
        self._ensure_index_paths_regular(root, changed_paths, index_file=index_file)
        if self._full_diff_has_secret(diff_bytes):
            raise CodingToolError("SECRET_CONTENT_DENIED", "The diff contains protected content.")
        return changed_paths

    def _validate_patch(self, root: Path, patch: str) -> tuple[bytes, list[str]]:
        if not isinstance(patch, str) or not patch:
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The patch must be non-empty text.")
        try:
            patch_bytes = patch.encode("utf-8")
        except UnicodeEncodeError as exception:
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The patch must be UTF-8 text.") from exception
        if len(patch_bytes) > MAX_TEXT_BYTES or b"\0" in patch_bytes:
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The patch is too large or binary.")
        forbidden_markers = (
            "GIT binary patch",
            "Binary files ",
            "rename from ",
            "rename to ",
            "copy from ",
            "copy to ",
            "similarity index ",
            "dissimilarity index ",
            "old mode ",
            "new mode ",
        )
        patch_paths: list[str] = []
        seen_paths: set[str] = set()
        current_path: str | None = None
        for line in patch.splitlines():
            if line.startswith(forbidden_markers):
                raise CodingToolError("PATCH_POLICY_DENIED", "The patch uses a forbidden Git feature.")
            if line.startswith("new file mode ") and line != "new file mode 100644":
                raise CodingToolError("PATCH_POLICY_DENIED", "Only regular text files may be added.")
            if line.startswith("deleted file mode ") and line != "deleted file mode 100644":
                raise CodingToolError("PATCH_POLICY_DENIED", "Only regular text files may be deleted.")
            match = DIFF_HEADER.fullmatch(line)
            if line.startswith("diff --git ") and match is None:
                raise CodingToolError("PATCH_POLICY_DENIED", "Patch paths must be canonical Git paths.")
            if match is not None:
                if match.group(1) != match.group(2):
                    raise CodingToolError("PATCH_POLICY_DENIED", "Rename-style patches are not allowed.")
                current_path = match.group(1)
                _validate_wire_path(current_path, allow_root=False)
                if current_path in seen_paths:
                    raise CodingToolError(
                        "PATCH_POLICY_DENIED",
                        "Each patch path may appear only once.",
                    )
                seen_paths.add(current_path)
                patch_paths.append(current_path)
                if len(patch_paths) > MAX_CHANGED_FILES:
                    raise CodingToolError(
                        "RESULT_TOO_LARGE",
                        "The patch changes too many files.",
                    )
                if _is_protected_path(current_path):
                    raise CodingToolError("PATH_POLICY_DENIED", "The patch contains a protected path.")
                self._target(root, current_path, must_exist=False)
            elif line.startswith(("--- ", "+++ ")):
                header_path = line[4:]
                if header_path == "/dev/null":
                    continue
                if not header_path.startswith(("a/", "b/")):
                    raise CodingToolError("PATCH_POLICY_DENIED", "Patch paths must be canonical Git paths.")
                relative = header_path[2:]
                _validate_wire_path(relative, allow_root=False)
                if current_path is None or relative != current_path:
                    raise CodingToolError(
                        "PATCH_POLICY_DENIED",
                        "Patch headers must match their canonical diff path.",
                    )
                if _is_protected_path(relative):
                    raise CodingToolError("PATH_POLICY_DENIED", "The patch contains a protected path.")
                self._target(root, relative, must_exist=False)
        if not patch_paths:
            raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The patch has no canonical diff header.")
        self._ensure_diff_modes_regular(patch_bytes)
        if self._full_diff_has_secret(patch_bytes):
            raise CodingToolError("SECRET_CONTENT_DENIED", "The patch contains protected content.")
        return patch_bytes, patch_paths

    @staticmethod
    def _ensure_changed_paths_safe(changed_paths: list[str]) -> None:
        if any(_is_protected_path(path) for path in changed_paths):
            raise CodingToolError("PATH_POLICY_DENIED", "The diff contains a protected path.")

    def _workspace_lock(self, root: Path) -> threading.Lock:
        key = os.path.normcase(str(root))
        with self._workspace_locks_guard:
            return self._workspace_locks.setdefault(key, threading.Lock())

    def _preflight_patch(
        self,
        root: Path,
        patch_bytes: bytes,
    ) -> tuple[bytes, list[str]]:
        index_path = self._index_path(root)
        try:
            with tempfile.TemporaryDirectory(prefix="axms-mcp-index-") as directory:
                temporary_index = Path(directory, "index")
                shutil.copyfile(index_path, temporary_index)
                self._run_git(
                    root,
                    "apply",
                    "--check",
                    "--cached",
                    "--whitespace=error-all",
                    "-",
                    input_bytes=patch_bytes,
                    index_file=temporary_index,
                )
                self._run_git(
                    root,
                    "apply",
                    "--cached",
                    "--whitespace=error-all",
                    "-",
                    input_bytes=patch_bytes,
                    index_file=temporary_index,
                )
                diff_bytes = self._diff_bytes(
                    root,
                    cached=True,
                    index_file=temporary_index,
                )
                changed_paths = self._validate_diff(
                    root,
                    diff_bytes,
                    cached=True,
                    index_file=temporary_index,
                )
                return diff_bytes, changed_paths
        except OSError as exception:
            raise CodingToolError(
                "TOOL_EXECUTION_FAILED",
                "The fixed Git patch preflight failed.",
            ) from exception

    def _index_path(self, root: Path) -> Path:
        result = self._run_git(root, "rev-parse", "--git-path", "index")
        configured = Path(self._decode_text(result.stdout).strip())
        candidate = configured if configured.is_absolute() else root / configured
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exception:
            raise CodingToolError(
                "REPOSITORY_SCOPE_DENIED",
                "The repository index is unavailable.",
            ) from exception
        if not resolved.is_file():
            raise CodingToolError(
                "REPOSITORY_SCOPE_DENIED",
                "The repository index is not a regular file.",
            )
        return resolved

    def _ensure_index_paths_regular(
        self,
        root: Path,
        paths: list[str],
        *,
        index_file: Path | None = None,
    ) -> None:
        for offset in range(0, len(paths), 32):
            requested = set(paths[offset : offset + 32])
            result = self._run_git(
                root,
                "ls-files",
                "--stage",
                "-z",
                "--",
                *sorted(requested),
                index_file=index_file,
            )
            seen: set[str] = set()
            for raw_entry in result.stdout.split(b"\0"):
                if not raw_entry:
                    continue
                entry = self._decode_text(raw_entry)
                metadata, separator, path = entry.partition("\t")
                fields = metadata.split(" ")
                if (
                    separator != "\t"
                    or len(fields) != 3
                    or fields[0] != "100644"
                    or fields[2] != "0"
                    or path not in requested
                    or path in seen
                ):
                    raise CodingToolError(
                        "PATCH_POLICY_DENIED",
                        "Only stage-zero regular 100644 files may be changed.",
                    )
                seen.add(path)

    def _ensure_diff_modes_regular(self, diff_bytes: bytes) -> None:
        for line in self._decode_text(diff_bytes).splitlines():
            if line.startswith(("old mode ", "new mode ")):
                raise CodingToolError(
                    "PATCH_POLICY_DENIED",
                    "File mode changes are not allowed.",
                )
            if line.startswith("new file mode ") and line != "new file mode 100644":
                raise CodingToolError(
                    "PATCH_POLICY_DENIED",
                    "Only regular 100644 files may be added.",
                )
            if line.startswith("deleted file mode ") and line != "deleted file mode 100644":
                raise CodingToolError(
                    "PATCH_POLICY_DENIED",
                    "Only regular 100644 files may be deleted.",
                )
            match = INDEX_MODE.fullmatch(line)
            if match is not None and match.group(1) != "100644":
                raise CodingToolError(
                    "PATCH_POLICY_DENIED",
                    "Gitlinks and non-100644 index entries are not allowed.",
                )

    def _ensure_repository_config_safe(self, root: Path) -> None:
        result = self._run_git(
            root,
            "config",
            "--includes",
            "--name-only",
            "--get-regexp",
            RISKY_GIT_CONFIG,
            allowed_return_codes=frozenset({0, 1}),
        )
        if result.returncode == 0 and result.stdout.strip():
            raise CodingToolError(
                "REPOSITORY_SCOPE_DENIED",
                "The repository contains an executable Git filter or diff command.",
            )

    def _ensure_index_matches_worktree(self, root: Path) -> None:
        unstaged = self._run_git(
            root,
            "diff",
            "--quiet",
            "--",
            allowed_return_codes=frozenset({0, 1}),
        )
        if unstaged.returncode != 0:
            raise CodingToolError("CONTEXT_DIGEST_MISMATCH", "The worktree has untracked state outside the index.")
        status = self._run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if any(entry.startswith(b"?? ") for entry in status.stdout.split(b"\0") if entry):
            raise CodingToolError("CONTEXT_DIGEST_MISMATCH", "The worktree contains an untracked file.")

    def _full_diff_has_secret(self, diff_bytes: bytes) -> bool:
        in_hunk = False
        for line in self._decode_text(diff_bytes).splitlines():
            if line.startswith("diff --git "):
                in_hunk = False
                continue
            if HUNK_HEADER.match(line):
                in_hunk = True
                continue
            if in_hunk and line.startswith((" ", "+", "-")):
                if self._secret_findings(line[1:], ""):
                    return True
        return False

    def _added_findings_for_diff(self, diff_bytes: bytes) -> list[SecretFinding]:
        text = self._decode_text(diff_bytes)
        current_path = ""
        current_line = 0
        findings: list[SecretFinding] = []
        for line in text.splitlines():
            if line.startswith("+++ "):
                value = line[4:]
                current_path = value[2:] if value.startswith("b/") else ""
                continue
            match = HUNK_HEADER.match(line)
            if match:
                current_line = int(match.group(1))
                continue
            if line.startswith("+") and not line.startswith("+++"):
                for line_number, rule in self._secret_findings(line[1:], current_path, current_line):
                    findings.append(
                        {"path": current_path, "line": line_number, "rule": rule}
                    )
                current_line += 1
            elif line.startswith("-") and not line.startswith("---"):
                continue
            elif current_path and not line.startswith("\\"):
                current_line += 1
        return findings[:100]

    @staticmethod
    def _secret_findings(text: str, path: str, fixed_line: int | None = None) -> list[tuple[int, str]]:
        findings: list[tuple[int, str]] = []
        for line_number, line in enumerate(text.splitlines() or [text], start=1):
            reported_line = fixed_line if fixed_line is not None else line_number
            for rule, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append((reported_line, rule))
        return findings

    def _read_bytes(self, path: Path, maximum: int) -> bytes:
        try:
            size = path.stat().st_size
            if size > maximum:
                raise CodingToolError("RESULT_TOO_LARGE", "The requested file is too large.")
            return path.read_bytes()
        except OSError as exception:
            raise CodingToolError("PATH_NOT_FOUND", "The requested file cannot be read.") from exception

    @staticmethod
    def _decode_text(value: bytes) -> str:
        if b"\0" in value:
            raise CodingToolError("BINARY_CONTENT_DENIED", "Binary content is not supported.")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exception:
            raise CodingToolError("BINARY_CONTENT_DENIED", "Only UTF-8 text is supported.") from exception

    @staticmethod
    def _files_below(root: Path, scope: Path) -> tuple[list[Path], bool]:
        files: list[Path] = []
        for directory, child_directories, child_files in os.walk(
            scope,
            topdown=True,
            followlinks=False,
        ):
            directory_path = Path(directory)
            allowed_directories: list[str] = []
            for name in sorted(child_directories):
                candidate = directory_path / name
                relative = candidate.relative_to(root).as_posix()
                if _is_protected_path(relative) or candidate.is_symlink() or _is_junction(candidate):
                    continue
                try:
                    if not candidate.resolve(strict=True).is_relative_to(root):
                        continue
                except OSError:
                    continue
                allowed_directories.append(name)
            child_directories[:] = allowed_directories
            for name in sorted(child_files):
                candidate = directory_path / name
                if len(files) == MAX_SEARCH_FILES:
                    return files, True
                relative = candidate.relative_to(root).as_posix()
                if _is_protected_path(relative) or candidate.is_symlink() or _is_junction(candidate):
                    continue
                try:
                    if not candidate.resolve(strict=True).is_relative_to(root):
                        continue
                except OSError:
                    continue
                if candidate.is_file():
                    files.append(candidate)
        return files, False

    @staticmethod
    def _safe_check_details(raw: bytes) -> str:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return "check output unavailable"
        safe: list[str] = []
        for line in text.splitlines():
            if line.startswith(("+", "-")) or any(pattern.search(line) for _, pattern in SECRET_PATTERNS):
                safe.append("[redacted]")
            else:
                safe.append(line[:500])
        return "\n".join(safe)

    @staticmethod
    def _safe_git_failure(stderr: bytes, root: Path) -> str:
        # Same discipline as _safe_check_details: never let file content or secret
        # material ride out on an error path. Additionally the workspace root is
        # masked, because a container path is nobody's business outside this host.
        try:
            text = stderr.decode("utf-8", errors="replace")
        except Exception:
            return ""
        safe: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            if line.startswith(("+", "-")) or any(
                pattern.search(line) for _, pattern in SECRET_PATTERNS
            ):
                safe.append("[redacted]")
            else:
                safe.append(line.replace(str(root), "<workspace>")[:200])
            if len(safe) >= 3:
                break
        # The Spring side truncates the refusal reason to 300 characters, so the
        # essential first "error:" line must fit inside that window with room left
        # for the prefix around it.
        return " | ".join(safe)[:240]

    def _run_git(
        self,
        root: Path,
        *arguments: str,
        input_bytes: bytes | None = None,
        allowed_return_codes: frozenset[int] = frozenset({0}),
        index_file: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"COMSPEC", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_PAGER": "cat",
                "LC_ALL": "C",
                "LANG": "C",
            }
        )
        if index_file is not None:
            if not index_file.is_absolute():
                raise CodingToolError(
                    "TOOL_EXECUTION_FAILED",
                    "The fixed Git index path is invalid.",
                )
            try:
                resolved_index = index_file.resolve(strict=True)
            except OSError as exception:
                raise CodingToolError(
                    "TOOL_EXECUTION_FAILED",
                    "The fixed Git index is unavailable.",
                ) from exception
            if not resolved_index.is_file():
                raise CodingToolError(
                    "TOOL_EXECUTION_FAILED",
                    "The fixed Git index is not a regular file.",
                )
            environment["GIT_INDEX_FILE"] = str(resolved_index)
        command = (
            str(self._git_executable),
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "color.ui=false",
            *arguments,
        )
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                input=input_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exception:
            raise CodingToolError("TOOL_EXECUTION_FAILED", "The fixed Git operation failed.") from exception
        if len(result.stdout) + len(result.stderr) > MAX_GIT_OUTPUT_BYTES:
            raise CodingToolError("RESULT_TOO_LARGE", "The fixed Git operation returned too much output.")
        if result.returncode not in allowed_return_codes:
            # git names the defect in stderr ("error: patch failed: PATH:LINE"). Dropping
            # it here left the Coding Model retrying the same broken patch blind, so the
            # reason travels along - masked and bounded by _safe_git_failure.
            reason = self._safe_git_failure(result.stderr, root)
            message = "The fixed Git operation failed."
            if reason:
                message = f"{message} git: {reason}"
            raise CodingToolError("TOOL_EXECUTION_FAILED", message)
        return result


def _validate_wire_path(wire_path: str, *, allow_root: bool) -> tuple[tuple[str, ...], str]:
    if not isinstance(wire_path, str) or len(wire_path) > 512 or _has_control(wire_path):
        raise CodingToolError("TOOL_ARGUMENTS_INVALID", "The repository path is invalid.")
    if wire_path == "." and allow_root:
        return (), "."
    if not wire_path or "\\" in wire_path or ":" in wire_path:
        raise CodingToolError("PATH_POLICY_DENIED", "The repository path is not canonical.")
    pure = PurePosixPath(wire_path)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise CodingToolError("PATH_POLICY_DENIED", "The repository path is outside policy.")
    canonical = pure.as_posix()
    if canonical != wire_path:
        raise CodingToolError("PATH_POLICY_DENIED", "The repository path is not canonical.")
    return pure.parts, canonical


def _validated_git_executable(configured: Path) -> Path:
    if not isinstance(configured, Path) or not configured.is_absolute():
        raise ValueError("The Git executable must be configured as an absolute path.")
    try:
        resolved = configured.resolve(strict=True)
    except OSError as exception:
        raise ValueError("The configured Git executable is unavailable.") from exception
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("The configured Git executable is not executable.")
    return resolved


def _is_protected_path(path: str) -> bool:
    pure_path = PurePosixPath(path)
    parts = tuple(part.lower() for part in pure_path.parts)
    if any(part in PROTECTED_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    if any(_protected_code_component(part) for part in pure_path.parts):
        return True
    name = parts[-1] if parts else ""
    suffix = Path(name).suffix.lower()
    if (
        name in PROTECTED_DIRECTORY_NAMES
        or name in PROTECTED_GIT_FILE_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or suffix in PROTECTED_SECRET_SUFFIXES
    ):
        return True
    if suffix == ".sql" and re.match(
        r"^(?:(?:v|u)[0-9][0-9_.-]*|r)__",
        name,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def _protected_code_component(component: str) -> bool:
    tokens: set[str] = set()
    for chunk in re.split(r"[^A-Za-z0-9]+", component):
        tokens.update(match.group(0).lower() for match in CAMEL_WORD.finditer(chunk))
    if tokens.intersection(PROTECTED_CODE_TOKENS):
        return True
    normalized = re.sub(r"[^a-z0-9]", "", Path(component).stem.lower())
    without_secretary = normalized.replace("secretary", "")
    return bool(
        PROTECTED_CODE_MORPHOLOGY.search(normalized)
        or "secret" in without_secretary
    )


def _is_dependency_file(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name in DEPENDENCY_FILE_NAMES or name == "libs.versions.toml"


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker()) if checker is not None else False


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
