from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
import shutil
import subprocess
import tempfile

from starlette.testclient import TestClient

from axms_mcp_server.config import Settings
from axms_mcp_server.common.catalog import PRODUCTION_TOOL_NAMES
from axms_mcp_server.service import PROTOCOL_VERSION, SERVER_NAME, create_application


SERVICE_TOKEN = "t" * 43
EMPTY_DIFF_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()


def _settings(workspace_root: Path = Path("/workspaces")) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=18091,
        allowed_hosts=("testserver", "localhost:*"),
        allowed_origins=(),
        max_request_body_size=4096,
        workspace_root=workspace_root,
        git_executable=_git_executable(),
    )


def _mcp_headers(**extra: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {SERVICE_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    headers.update(extra)
    return headers


def _request(request_id: str, method: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {
                    "name": "ax-module-studio-backend-test",
                    "version": "0.1.0",
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }


def _tool_request(
    request_id: str,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    request = _request(request_id, "tools/call")
    params = request["params"]
    assert isinstance(params, dict)
    params.update({"name": name, "arguments": arguments})
    return request


class McpServiceTest(unittest.TestCase):
    def test_health_routes_are_public_and_ready(self) -> None:
        with TestClient(create_application(_settings(), SERVICE_TOKEN)) as client:
            live = client.get("/health/live")
            ready = client.get("/health/ready")

        self.assertEqual(200, live.status_code)
        self.assertEqual({"status": "UP", "service": SERVER_NAME}, live.json())
        self.assertEqual(200, ready.status_code)
        self.assertEqual("READY", ready.json()["status"])
        self.assertEqual(PROTOCOL_VERSION, ready.json()["protocolVersion"])
        self.assertEqual(13, ready.json()["registeredToolCount"])

    def test_mcp_endpoint_requires_exact_bearer_token(self) -> None:
        application = create_application(_settings(), SERVICE_TOKEN)
        with TestClient(application) as client:
            missing = client.post("/mcp", json={})
            wrong = client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})

        self.assertEqual(401, missing.status_code)
        self.assertEqual("Bearer", missing.headers["WWW-Authenticate"])
        self.assertEqual(401, wrong.status_code)

    def test_authenticated_read_file_tool_call_returns_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace_root = Path(directory)
            repository = workspace_root / "job-1"
            repository.mkdir()
            _git(repository, "init", "--initial-branch=main")
            _git(repository, "config", "user.name", "AXMS Test")
            _git(repository, "config", "user.email", "axms-test@example.invalid")
            _git(repository, "config", "core.autocrlf", "false")
            (repository / "README.md").write_text(
                "tool round trip\n",
                encoding="utf-8",
                newline="\n",
            )
            _git(repository, "add", "README.md")
            _git(repository, "commit", "-m", "test baseline")
            head = _git(repository, "rev-parse", "HEAD").stdout.decode().strip()
            request = _tool_request(
                "read",
                "read_file",
                {"workspace": "job-1", "expectedHead": head, "path": "README.md"},
            )
            patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-tool round trip
+tool mutation round trip
"""

            with TestClient(
                create_application(_settings(workspace_root), SERVICE_TOKEN)
            ) as client:
                discovered = client.post(
                    "/mcp",
                    json=_request("discover-before-call", "server/discover"),
                    headers=_mcp_headers(**{"Mcp-Method": "server/discover"}),
                )
                authenticated = client.post(
                    "/mcp",
                    json=request,
                    headers=_mcp_headers(
                        **{"Mcp-Method": "tools/call", "Mcp-Name": "read_file"}
                    ),
                )
                applied = client.post(
                    "/mcp",
                    json=_tool_request(
                        "apply",
                        "apply_patch",
                        {
                            "workspace": "job-1",
                            "expectedHead": head,
                            "expectedDiffDigest": EMPTY_DIFF_DIGEST,
                            "patch": patch,
                        },
                    ),
                    headers=_mcp_headers(
                        **{"Mcp-Method": "tools/call", "Mcp-Name": "apply_patch"}
                    ),
                )
                diff = client.post(
                    "/mcp",
                    json=_tool_request(
                        "diff",
                        "read_diff",
                        {"workspace": "job-1", "expectedHead": head},
                    ),
                    headers=_mcp_headers(
                        **{"Mcp-Method": "tools/call", "Mcp-Name": "read_diff"}
                    ),
                )
                extra_argument = client.post(
                    "/mcp",
                    json=_tool_request(
                        "extra-argument",
                        "run_check",
                        {
                            "workspace": "job-1",
                            "expectedHead": head,
                            "expectedDiffDigest": applied.json()["result"][
                                "structuredContent"
                            ]["diffDigest"],
                            "profile": "git-diff-check",
                            "command": "not-allowed",
                        },
                    ),
                    headers=_mcp_headers(
                        **{"Mcp-Method": "tools/call", "Mcp-Name": "run_check"}
                    ),
                )
                unauthenticated = client.post(
                    "/mcp",
                    json=request,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": PROTOCOL_VERSION,
                        "Mcp-Method": "tools/call",
                        "Mcp-Name": "read_file",
                    },
                )

        self.assertEqual(200, discovered.status_code, discovered.text)
        self.assertIn(PROTOCOL_VERSION, discovered.json()["result"]["supportedVersions"])
        self.assertEqual(200, authenticated.status_code, authenticated.text)
        result = authenticated.json()["result"]
        self.assertFalse(result["isError"])
        self.assertEqual("tool round trip\n", result["structuredContent"]["content"])
        self.assertEqual("README.md", result["structuredContent"]["path"])
        self.assertEqual(200, applied.status_code, applied.text)
        applied_result = applied.json()["result"]
        self.assertFalse(applied_result["isError"])
        self.assertEqual(
            ["README.md"], applied_result["structuredContent"]["changedPaths"]
        )
        self.assertEqual(200, diff.status_code, diff.text)
        diff_result = diff.json()["result"]
        self.assertFalse(diff_result["isError"])
        self.assertEqual(
            applied_result["structuredContent"]["diffDigest"],
            diff_result["structuredContent"]["digest"],
        )
        self.assertIn(
            "+tool mutation round trip",
            diff_result["structuredContent"]["diff"],
        )
        self.assertEqual(200, extra_argument.status_code, extra_argument.text)
        self.assertTrue(extra_argument.json()["result"]["isError"])
        self.assertEqual(401, unauthenticated.status_code)

    def test_transport_rejects_invalid_host_and_origin(self) -> None:
        with TestClient(create_application(_settings(), SERVICE_TOKEN)) as client:
            invalid_host = client.post(
                "/mcp",
                json=_request("host", "server/discover"),
                headers=_mcp_headers(Host="untrusted.example", **{"Mcp-Method": "server/discover"}),
            )
            invalid_origin = client.post(
                "/mcp",
                json=_request("origin", "server/discover"),
                headers=_mcp_headers(Origin="https://untrusted.example", **{"Mcp-Method": "server/discover"}),
            )

        self.assertEqual(421, invalid_host.status_code)
        self.assertEqual(403, invalid_origin.status_code)

    def test_modern_discovery_and_feature_tools_catalog_round_trip(self) -> None:
        with TestClient(create_application(_settings(), SERVICE_TOKEN)) as client:
            discovery = client.post(
                "/mcp",
                json=_request("discover", "server/discover"),
                headers=_mcp_headers(**{"Mcp-Method": "server/discover"}),
            )
            tools = client.post(
                "/mcp",
                json=_request("tools", "tools/list"),
                headers=_mcp_headers(**{"Mcp-Method": "tools/list"}),
            )

        self.assertEqual(200, discovery.status_code, discovery.text)
        discovery_result = discovery.json()["result"]
        self.assertIn(PROTOCOL_VERSION, discovery_result["supportedVersions"])
        self.assertEqual(
            SERVER_NAME,
            discovery_result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"],
        )
        self.assertEqual(200, tools.status_code, tools.text)
        registered = tools.json()["result"]["tools"]
        self.assertEqual(list(PRODUCTION_TOOL_NAMES), [tool["name"] for tool in registered])
        expected_required = {
            "read_file": {"workspace", "expectedHead", "path"},
            "search_code": {"workspace", "expectedHead", "query"},
            "read_diff": {"workspace", "expectedHead"},
            "apply_patch": {"workspace", "expectedHead", "expectedDiffDigest", "patch"},
            "run_check": {"workspace", "expectedHead", "expectedDiffDigest", "profile"},
            "check_package_allowlist": {"workspace", "expectedHead", "expectedDiffDigest"},
            "scan_changed_files": {"workspace", "expectedHead", "expectedDiffDigest"},
            "resolve_cms_target": {"resource", "currentState"},
            "validate_cms_command": {"resource", "command", "currentState"},
            "create_cms_preview": {"resource", "command", "currentState"},
            "discard_cms_preview": {"previewId", "previewHash"},
            "revalidate_cms_preview": {
                "previewId", "previewHash", "resource", "command", "currentState"
            },
            "apply_cms_preview": {
                "previewId", "previewHash", "resource", "command", "currentState"
            },
        }
        expected_properties = {
            "read_file": {"workspace", "expectedHead", "path"},
            "search_code": {"workspace", "expectedHead", "query", "scope"},
            "read_diff": {"workspace", "expectedHead"},
            "apply_patch": {"workspace", "expectedHead", "expectedDiffDigest", "patch"},
            "run_check": {"workspace", "expectedHead", "expectedDiffDigest", "profile"},
            "check_package_allowlist": {"workspace", "expectedHead", "expectedDiffDigest"},
            "scan_changed_files": {"workspace", "expectedHead", "expectedDiffDigest"},
            "resolve_cms_target": {"resource", "currentState"},
            "validate_cms_command": {"resource", "command", "currentState"},
            "create_cms_preview": {"resource", "command", "currentState"},
            "discard_cms_preview": {"previewId", "previewHash"},
            "revalidate_cms_preview": {
                "previewId", "previewHash", "resource", "command", "currentState"
            },
            "apply_cms_preview": {
                "previewId", "previewHash", "resource", "command", "currentState"
            },
        }
        for tool in registered:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(
                    expected_required[tool["name"]],
                    set(tool["inputSchema"].get("required", [])),
                )
                self.assertEqual("object", tool["inputSchema"]["type"])
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
                self.assertEqual(
                    expected_properties[tool["name"]],
                    set(tool["inputSchema"]["properties"]),
                )
                self.assertEqual("object", tool["outputSchema"]["type"])


def _git_executable() -> Path:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("Git is required for the MCP test suite.")
    return Path(executable).resolve(strict=True)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (str(_git_executable()), *arguments),
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        shell=False,
    )


if __name__ == "__main__":
    unittest.main()
