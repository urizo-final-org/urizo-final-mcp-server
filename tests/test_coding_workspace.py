from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
import unittest

from axms_mcp_server.coding.workspace import CodingToolError, CodingWorkspace


EMPTY_DIFF_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()
GIT_EXECUTABLE = Path(shutil.which("git") or "git-not-found").resolve(strict=True)


class CodingWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temporary.name)
        self.repository = self.workspace_root / "job-1"
        self.repository.mkdir()
        self._git("init", "--initial-branch=main")
        self._git("config", "user.name", "AXMS Test")
        self._git("config", "user.email", "axms-test@example.invalid")
        self._git("config", "core.autocrlf", "false")
        (self.repository / "src").mkdir()
        (self.repository / "README.md").write_text(
            "hello workspace\n", encoding="utf-8", newline="\n"
        )
        (self.repository / "src" / "app.py").write_text(
            "print('hello')\n", encoding="utf-8", newline="\n"
        )
        self._git("add", "README.md", "src/app.py")
        self._git("commit", "-m", "test baseline")
        self.head = self._git("rev-parse", "HEAD").stdout.decode().strip()
        self.service = CodingWorkspace(self.workspace_root, GIT_EXECUTABLE)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_git_executable_is_absolute_fixed_and_validated_at_creation(self) -> None:
        missing = self.workspace_root / "missing-git"
        with self.assertRaisesRegex(ValueError, "unavailable"):
            CodingWorkspace(self.workspace_root, missing)
        with self.assertRaisesRegex(ValueError, "absolute path"):
            CodingWorkspace(self.workspace_root, Path("git"))

    def test_read_search_apply_diff_and_safe_checks(self) -> None:
        read = self.service.read_file("job-1", self.head, "README.md")
        self.assertEqual("hello workspace\n", read["content"])
        self.assertEqual("text/plain; charset=utf-8", read["mediaType"])

        search = self.service.search_code("job-1", self.head, "hello", "src")
        self.assertEqual(
            [{"path": "src/app.py", "line": 1, "column": 8, "preview": "print('hello')"}],
            search["matches"],
        )

        applied = self.service.apply_patch(
            "job-1",
            self.head,
            EMPTY_DIFF_DIGEST,
            _replace_app_patch("print('changed')"),
        )
        self.assertEqual(["src/app.py"], applied["changedPaths"])
        diff = self.service.read_diff("job-1", self.head)
        self.assertEqual(applied["diffDigest"], diff["digest"])
        self.assertIn("+print('changed')", diff["diff"])

        syntax = self.service.run_check(
            "job-1", self.head, diff["digest"], "python-syntax"
        )
        whitespace = self.service.run_check(
            "job-1", self.head, diff["digest"], "git-diff-check"
        )
        packages = self.service.check_package_allowlist(
            "job-1", self.head, diff["digest"]
        )
        scan = self.service.scan_changed_files("job-1", self.head, diff["digest"])
        self.assertEqual("PASSED", syntax["status"])
        self.assertEqual("PASSED", whitespace["status"])
        self.assertTrue(packages["passed"])
        self.assertTrue(scan["passed"])

    def test_apply_patch_stages_new_file_for_complete_diff(self) -> None:
        patch = """diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new content
"""
        applied = self.service.apply_patch(
            "job-1", self.head, EMPTY_DIFF_DIGEST, patch
        )
        self.assertEqual(["new.txt"], applied["changedPaths"])
        self.assertEqual("A  new.txt", self._git("status", "--short").stdout.decode().strip())
        self.assertIn("new file mode 100644", self.service.read_diff("job-1", self.head)["diff"])

    def test_stale_head_and_diff_digest_are_rejected(self) -> None:
        with self.assertRaisesRegex(CodingToolError, "Git head changed") as head_failure:
            self.service.read_file("job-1", "0" * 40, "README.md")
        self.assertEqual("CANDIDATE_SHA_MISMATCH", head_failure.exception.code)

        with self.assertRaisesRegex(CodingToolError, "workspace diff changed") as diff_failure:
            self.service.apply_patch(
                "job-1",
                self.head,
                "sha256:" + "0" * 64,
                _replace_app_patch("print('changed')"),
            )
        self.assertEqual("CONTEXT_DIGEST_MISMATCH", diff_failure.exception.code)

    def test_git_refusal_carries_its_reason_masked(self) -> None:
        # The measured failure mode: a hunk whose context does not exist in the real
        # file. (Line numbers alone are not enough to trip git - when the context
        # matches, git apply finds the hunk by content and applies it at an offset.)
        # git names the file and line in stderr; that reason must reach the caller
        # instead of being dropped, with the container path masked.
        patch = (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -177,1 +177,2 @@\n"
            " context that the file never contained\n"
            "+added line\n"
        )
        with self.assertRaisesRegex(CodingToolError, "git:") as failure:
            self.service.apply_patch("job-1", self.head, EMPTY_DIFF_DIGEST, patch)
        self.assertEqual("TOOL_EXECUTION_FAILED", failure.exception.code)
        message = str(failure.exception)
        self.assertIn("README.md", message)
        self.assertNotIn(str(self.repository), message)
        self.assertNotIn(self.temporary.name, message)

    def test_safe_git_failure_masks_secrets_paths_and_bounds_length(self) -> None:
        root = self.repository
        stderr = (
            f"error: patch failed: {root}/src/app.py:7\n"
            "token = abcdef1234567890abcdef\n"
            "error: " + "x" * 500 + "\n"
            "one line too many\n"
        ).encode("utf-8")
        safe = CodingWorkspace._safe_git_failure(stderr, root)
        self.assertIn("<workspace>/src/app.py:7", safe)
        self.assertNotIn(str(root), safe)
        self.assertNotIn("abcdef1234567890abcdef", safe)
        self.assertIn("[redacted]", safe)
        self.assertNotIn("one line too many", safe)
        self.assertLessEqual(len(safe), 240)

    def test_path_confinement_and_fixed_protected_paths(self) -> None:
        denied = (
            "../README.md",
            "/etc/passwd",
            "src\\app.py",
            ".git/config",
            ".gitattributes",
            ".gitmodules",
            ".env",
            "secrets/token.txt",
            "src/security/SecurityConfig.java",
            "src/auth.py",
            "src/main/java/AuthConfig.java",
            "src/main/java/AuthenticationService.java",
            "src/main/java/CredentialStore.java",
            "src/main/java/SecretsManager.java",
            "src/main/java/authz/AuthorizationService.java",
            "src/main/java/UserAuthService.java",
            "src/main/java/JwtAuthorizationPolicy.java",
            "src/main/java/ApiCredentialStore.java",
            "src/main/java/RuntimeSecretsProvider.java",
            "src/main/java/authconfig.java",
            "src/main/java/AuthenticatedActor.java",
            "src/main/java/Authenticator.java",
            "src/main/java/AuthorizeRequest.java",
            "src/main/java/AuthorityService.java",
            "src/main/java/securityfilter.java",
            "src/main/java/CredentialedClient.java",
            "src/main/java/SecretiveThing.java",
            "src/main/resources/db/migration/V1__schema.sql",
            "private.pem",
        )
        for path in denied:
            with self.subTest(path=path):
                with self.assertRaises(CodingToolError) as failure:
                    self.service.read_file("job-1", self.head, path)
                self.assertEqual("PATH_POLICY_DENIED", failure.exception.code)

        allowed_sources = {
            "AuthorService.java": "final class AuthorService {}\n",
            "Secretary.java": "final class Secretary {}\n",
            "Tokenize.java": "final class Tokenize {}\n",
        }
        source_directory = self.repository / "src" / "main" / "java"
        source_directory.mkdir(parents=True)
        for name, content in allowed_sources.items():
            (source_directory / name).write_text(
                content,
                encoding="utf-8",
                newline="\n",
            )
        self._git("add", *(f"src/main/java/{name}" for name in allowed_sources))
        self._git("commit", "-m", "allowed author fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.decode().strip()
        for name in allowed_sources:
            with self.subTest(allowed=name):
                self.assertIn(
                    Path(name).stem,
                    self.service.read_file(
                        "job-1", self.head, f"src/main/java/{name}"
                    )["content"],
                )

    def test_linked_path_cannot_escape_workspace(self) -> None:
        outside = self.workspace_root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8", newline="\n")
        link = self.repository / "src" / "linked.txt"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("Symbolic links are unavailable on this test host.")
        with self.assertRaises(CodingToolError) as linked:
            self.service.read_file("job-1", self.head, "src/linked.txt")
        self.assertEqual("PATH_POLICY_DENIED", linked.exception.code)

    def test_repository_git_filter_commands_fail_closed_without_execution(self) -> None:
        self._git("config", "filter.unsafe.clean", "definitely-not-a-command")
        with self.assertRaises(CodingToolError) as failure:
            self.service.read_file("job-1", self.head, "README.md")
        self.assertEqual("REPOSITORY_SCOPE_DENIED", failure.exception.code)

    def test_patch_policy_rejects_secret_migration_mode_and_rename_without_mutation(self) -> None:
        denied_patches = (
            _new_file_patch("src/config.py", 'api_key = "abcdefghijklmnopqrstuvwxyz"'),
            _new_file_patch(".gitattributes", "*.py filter=unsafe"),
            _new_file_patch("src/main/java/AuthConfig.java", "final class AuthConfig {}"),
            _new_file_patch(
                "src/main/java/AuthenticationService.java",
                "final class AuthenticationService {}",
            ),
            _new_file_patch(
                "src/main/java/CredentialStore.java",
                "final class CredentialStore {}",
            ),
            _new_file_patch(
                "src/main/java/SecretsManager.java",
                "final class SecretsManager {}",
            ),
            _new_file_patch(
                "src/main/java/authz/AuthorizationService.java",
                "final class AuthorizationService {}",
            ),
            _new_file_patch(
                "src/main/java/UserAuthService.java",
                "final class UserAuthService {}",
            ),
            _new_file_patch(
                "src/main/java/JwtAuthorizationPolicy.java",
                "final class JwtAuthorizationPolicy {}",
            ),
            _new_file_patch(
                "src/main/java/ApiCredentialStore.java",
                "final class ApiCredentialStore {}",
            ),
            _new_file_patch(
                "src/main/java/RuntimeSecretsProvider.java",
                "final class RuntimeSecretsProvider {}",
            ),
            _new_file_patch(
                "src/main/java/authconfig.java",
                "final class authconfig {}",
            ),
            _new_file_patch(
                "src/main/java/AuthenticatedActor.java",
                "final class AuthenticatedActor {}",
            ),
            _new_file_patch(
                "src/main/java/Authenticator.java",
                "final class Authenticator {}",
            ),
            _new_file_patch(
                "src/main/java/AuthorizeRequest.java",
                "final class AuthorizeRequest {}",
            ),
            _new_file_patch(
                "src/main/java/AuthorityService.java",
                "final class AuthorityService {}",
            ),
            _new_file_patch(
                "src/main/java/securityfilter.java",
                "final class securityfilter {}",
            ),
            _new_file_patch(
                "src/main/java/CredentialedClient.java",
                "final class CredentialedClient {}",
            ),
            _new_file_patch(
                "src/main/java/SecretiveThing.java",
                "final class SecretiveThing {}",
            ),
            _new_file_patch("src/main/resources/db/migration/V2__unsafe.sql", "select 1;"),
            """diff --git a/link b/link
new file mode 120000
--- /dev/null
+++ b/link
@@ -0,0 +1 @@
+README.md
""",
            """diff --git a/executable.sh b/executable.sh
new file mode 100755
--- /dev/null
+++ b/executable.sh
@@ -0,0 +1 @@
+echo denied
""",
            """diff --git a/README.md b/README.md
deleted file mode 120000
--- a/README.md
+++ /dev/null
@@ -1 +0,0 @@
-hello workspace
""",
            """diff --git \"a/unsafe name.txt\" \"b/unsafe name.txt\"
new file mode 100644
--- /dev/null
+++ \"b/unsafe name.txt\"
@@ -0,0 +1 @@
+denied
""",
            """diff --git a/README.md b/MOVED.md
similarity index 100%
rename from README.md
rename to MOVED.md
""",
        )
        original = (self.repository / "src" / "app.py").read_text(encoding="utf-8")
        for patch in denied_patches:
            with self.subTest(patch=patch.splitlines()[0]):
                with self.assertRaises(CodingToolError):
                    self.service.apply_patch("job-1", self.head, EMPTY_DIFF_DIGEST, patch)
                self.assertEqual(
                    original,
                    (self.repository / "src" / "app.py").read_text(encoding="utf-8"),
                )
                self.assertEqual("", self._git("status", "--short").stdout.decode())

    def test_untracked_file_prevents_patch_without_partial_mutation(self) -> None:
        (self.repository / "untracked.txt").write_text(
            "untracked\n", encoding="utf-8", newline="\n"
        )
        before = (self.repository / "src" / "app.py").read_text(encoding="utf-8")
        with self.assertRaises(CodingToolError) as failure:
            self.service.apply_patch(
                "job-1", self.head, EMPTY_DIFF_DIGEST, _replace_app_patch("print('changed')")
            )
        self.assertEqual("CONTEXT_DIGEST_MISMATCH", failure.exception.code)
        self.assertEqual(before, (self.repository / "src" / "app.py").read_text(encoding="utf-8"))

    def test_package_lock_and_redacted_secret_scan(self) -> None:
        (self.repository / "package.json").write_text(
            '{"name":"demo"}\n', encoding="utf-8", newline="\n"
        )
        self._git("add", "package.json")
        digest = _current_diff_digest(self.repository)
        packages = self.service.check_package_allowlist("job-1", self.head, digest)
        self.assertFalse(packages["passed"])
        self.assertEqual(["package.json"], packages["changedManifests"])

        self._git("reset", "--hard", "HEAD")
        (self.repository / "src" / "app.py").write_text(
            'api_key = "abcdefghijklmnopqrstuvwxyz"\n', encoding="utf-8", newline="\n"
        )
        self._git("add", "src/app.py")
        digest = _current_diff_digest(self.repository)
        scan = self.service.scan_changed_files("job-1", self.head, digest)
        self.assertFalse(scan["passed"])
        self.assertEqual("SECRET_ASSIGNMENT", scan["findings"][0]["rule"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", repr(scan))
        with self.assertRaises(CodingToolError) as diff_failure:
            self.service.read_diff("job-1", self.head)
        self.assertEqual("SECRET_CONTENT_DENIED", diff_failure.exception.code)

        readme_before = (self.repository / "README.md").read_bytes()
        with self.assertRaises(CodingToolError) as patch_failure:
            self.service.apply_patch(
                "job-1",
                self.head,
                digest,
                _replace_readme_patch("safe change"),
            )
        self.assertEqual("SECRET_CONTENT_DENIED", patch_failure.exception.code)
        self.assertEqual(readme_before, (self.repository / "README.md").read_bytes())

    def test_read_diff_rejects_deleted_and_context_secrets_while_scan_is_added_only(self) -> None:
        settings = self.repository / "src" / "settings.py"
        settings.write_text(
            'api_key = "abcdefghijklmnopqrstuvwxyz"\nvalue = 1\n',
            encoding="utf-8",
            newline="\n",
        )
        self._git("add", "src/settings.py")
        self._git("commit", "-m", "secret baseline fixture")
        self.head = self._git("rev-parse", "HEAD").stdout.decode().strip()

        settings.write_text("value = 1\n", encoding="utf-8", newline="\n")
        self._git("add", "src/settings.py")
        deleted_digest = _current_diff_digest(self.repository)
        deleted_scan = self.service.scan_changed_files(
            "job-1", self.head, deleted_digest
        )
        self.assertTrue(deleted_scan["passed"])
        with self.assertRaises(CodingToolError) as deleted_failure:
            self.service.read_diff("job-1", self.head)
        self.assertEqual("SECRET_CONTENT_DENIED", deleted_failure.exception.code)

        self._git("reset", "--hard", "HEAD")
        settings.write_text(
            'api_key = "abcdefghijklmnopqrstuvwxyz"\nvalue = 2\n',
            encoding="utf-8",
            newline="\n",
        )
        self._git("add", "src/settings.py")
        context_digest = _current_diff_digest(self.repository)
        context_scan = self.service.scan_changed_files(
            "job-1", self.head, context_digest
        )
        self.assertTrue(context_scan["passed"])
        with self.assertRaises(CodingToolError) as context_failure:
            self.service.read_diff("job-1", self.head)
        self.assertEqual("SECRET_CONTENT_DENIED", context_failure.exception.code)

    def test_patch_over_500_unique_paths_fails_without_index_or_worktree_mutation(self) -> None:
        patch = "".join(
            _minimal_new_file_patch(f"f/{index:03}")
            for index in range(501)
        )
        before = self._repository_state()
        with self.assertRaises(CodingToolError) as failure:
            self.service.apply_patch("job-1", self.head, EMPTY_DIFF_DIGEST, patch)
        self.assertEqual("RESULT_TOO_LARGE", failure.exception.code)
        self.assertEqual(before, self._repository_state())
        self.assertFalse((self.repository / "f").exists())

    def test_concurrent_patches_are_serialized_per_workspace(self) -> None:
        barrier = threading.Barrier(2)

        def apply(patch: str) -> str:
            barrier.wait()
            try:
                self.service.apply_patch(
                    "job-1",
                    self.head,
                    EMPTY_DIFF_DIGEST,
                    patch,
                )
                return "APPLIED"
            except CodingToolError as exception:
                return exception.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    apply,
                    (
                        _replace_app_patch("print('concurrent')"),
                        _replace_readme_patch("concurrent"),
                    ),
                )
            )

        self.assertCountEqual(["APPLIED", "CONTEXT_DIGEST_MISMATCH"], results)
        self.assertEqual(1, len(self._git("diff", "--name-only", "HEAD", "--").stdout.splitlines()))
        self._git("diff", "--quiet", "--")

    def test_concurrent_scan_and_apply_each_observe_one_consistent_snapshot(self) -> None:
        (self.repository / "src" / "app.py").write_text(
            "print('scan snapshot')\n",
            encoding="utf-8",
            newline="\n",
        )
        self._git("add", "src/app.py")
        initial_digest = _current_diff_digest(self.repository)
        scan_entered = threading.Event()
        release_scan = threading.Event()
        apply_started = threading.Event()
        original_scan = self.service._added_findings_for_diff

        def blocking_scan(diff_bytes: bytes) -> list[dict[str, object]]:
            scan_entered.set()
            if not release_scan.wait(timeout=5):
                raise RuntimeError("Concurrent scan test timed out.")
            return original_scan(diff_bytes)  # type: ignore[return-value]

        def apply() -> dict[str, object]:
            apply_started.set()
            return self.service.apply_patch(
                "job-1",
                self.head,
                initial_digest,
                _replace_readme_patch("scan apply snapshot"),
            )

        self.service._added_findings_for_diff = blocking_scan  # type: ignore[method-assign]
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                scan_future = executor.submit(
                    self.service.scan_changed_files,
                    "job-1",
                    self.head,
                    initial_digest,
                )
                self.assertTrue(scan_entered.wait(timeout=5))
                apply_future = executor.submit(apply)
                self.assertTrue(apply_started.wait(timeout=5))
                threading.Event().wait(0.1)
                self.assertFalse(apply_future.done())
                release_scan.set()
                scan = scan_future.result(timeout=5)
                applied = apply_future.result(timeout=5)
        finally:
            release_scan.set()
            self.service._added_findings_for_diff = original_scan  # type: ignore[method-assign]

        self.assertEqual(initial_digest, scan["diffDigest"])
        self.assertEqual(["src/app.py"], scan["changedPaths"])
        self.assertTrue(scan["passed"])
        self.assertEqual([], scan["findings"])
        self.assertEqual(["README.md", "src/app.py"], applied["changedPaths"])
        final_diff = self.service.read_diff("job-1", self.head)
        self.assertEqual(applied["diffDigest"], final_diff["digest"])

    def test_non_regular_executable_and_gitlink_targets_are_rejected(self) -> None:
        script = self.repository / "script.sh"
        script.write_text("echo baseline\n", encoding="utf-8", newline="\n")
        nested = self.repository / "vendor" / "module"
        nested.mkdir(parents=True)
        _git_at(nested, "init", "--initial-branch=main")
        _git_at(nested, "config", "user.name", "AXMS Test")
        _git_at(nested, "config", "user.email", "axms-test@example.invalid")
        _git_at(nested, "config", "core.autocrlf", "false")
        (nested / "module.txt").write_text(
            "module\n", encoding="utf-8", newline="\n"
        )
        _git_at(nested, "add", "module.txt")
        _git_at(nested, "commit", "-m", "nested baseline")
        nested_head = _git_at(nested, "rev-parse", "HEAD").stdout.decode().strip()

        self._git("add", "script.sh", "vendor/module")
        self._git("update-index", "--chmod=+x", "script.sh")
        self._git("commit", "-m", "non-regular mode fixtures")
        self.head = self._git("rev-parse", "HEAD").stdout.decode().strip()
        before = self._repository_state()

        executable_patch = """diff --git a/script.sh b/script.sh
--- a/script.sh
+++ b/script.sh
@@ -1 +1 @@
-echo baseline
+echo changed
"""
        gitlink_patch = f"""diff --git a/vendor/module b/vendor/module
--- a/vendor/module
+++ b/vendor/module
@@ -1 +1 @@
-Subproject commit {nested_head}
+Subproject commit {'1' * 40}
"""
        for patch in (executable_patch, gitlink_patch):
            with self.subTest(path=patch.splitlines()[0]):
                with self.assertRaises(CodingToolError) as failure:
                    self.service.apply_patch(
                        "job-1", self.head, EMPTY_DIFF_DIGEST, patch
                    )
                self.assertEqual("PATCH_POLICY_DENIED", failure.exception.code)
                self.assertEqual(before, self._repository_state())

    def test_check_profiles_are_fixed_and_do_not_import_python(self) -> None:
        (self.repository / "src" / "app.py").write_text(
            "def broken(:\n", encoding="utf-8", newline="\n"
        )
        self._git("add", "src/app.py")
        digest = _current_diff_digest(self.repository)
        syntax = self.service.run_check("job-1", self.head, digest, "python-syntax")
        self.assertEqual("FAILED", syntax["status"])
        self.assertIn("src/app.py:1", syntax["details"])

        with self.assertRaises(CodingToolError) as profile_failure:
            self.service.run_check("job-1", self.head, digest, "python-test; whoami")
        self.assertEqual("CHECK_PROFILE_NOT_ALLOWED", profile_failure.exception.code)

    def test_search_is_literal_bounded_and_skips_protected_content(self) -> None:
        lines = [f"needle {index}" for index in range(120)]
        (self.repository / "many.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
        (self.repository / "A-leak.txt").write_text(
            "-----BEGIN PRIVATE KEY-----\nneedle payload\n",
            encoding="utf-8",
            newline="\n",
        )
        self._git("add", "A-leak.txt", "many.txt")
        result = self.service.search_code("job-1", self.head, "needle", ".")
        self.assertEqual(100, len(result["matches"]))
        self.assertTrue(result["truncated"])
        self.assertNotIn("A-leak.txt", {match["path"] for match in result["matches"]})

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            (str(GIT_EXECUTABLE), *arguments),
            cwd=self.repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            shell=False,
        )

    def _repository_state(self) -> tuple[bytes, bytes, bytes]:
        return (
            self._git("diff", "--binary", "HEAD", "--").stdout,
            self._git("status", "--porcelain=v1", "-z", "--untracked-files=all").stdout,
            self._git("ls-files", "--stage", "-z").stdout,
        )


def _replace_app_patch(replacement: str) -> str:
    return f"""diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1 @@
-print('hello')
+{replacement}
"""


def _replace_readme_patch(replacement: str) -> str:
    return f"""diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-hello workspace
+{replacement}
"""


def _new_file_patch(path: str, content: str) -> str:
    return f"""diff --git a/{path} b/{path}
new file mode 100644
--- /dev/null
+++ b/{path}
@@ -0,0 +1 @@
+{content}
"""


def _minimal_new_file_patch(path: str) -> str:
    return f"""diff --git a/{path} b/{path}
--- /dev/null
+++ b/{path}
@@ -0,0 +1 @@
+x
"""


def _current_diff_digest(repository: Path) -> str:
    result = subprocess.run(
        (
            str(GIT_EXECUTABLE),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--text",
            "HEAD",
            "--",
        ),
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )
    return "sha256:" + hashlib.sha256(result.stdout).hexdigest()


def _git_at(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (str(GIT_EXECUTABLE), *arguments),
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )


if __name__ == "__main__":
    unittest.main()
