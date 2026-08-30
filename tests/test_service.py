from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from axms_mcp_server.config import Settings
from axms_mcp_server.service import PROTOCOL_VERSION, SERVER_NAME, create_application


SERVICE_TOKEN = "t" * 43


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=18091,
        allowed_hosts=("testserver", "localhost:*"),
        allowed_origins=(),
        max_request_body_size=4096,
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
        self.assertEqual(0, ready.json()["registeredToolCount"])

    def test_mcp_endpoint_requires_exact_bearer_token(self) -> None:
        application = create_application(_settings(), SERVICE_TOKEN)
        with TestClient(application) as client:
            missing = client.post("/mcp", json={})
            wrong = client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})

        self.assertEqual(401, missing.status_code)
        self.assertEqual("Bearer", missing.headers["WWW-Authenticate"])
        self.assertEqual(401, wrong.status_code)

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

    def test_modern_discovery_and_empty_tools_catalog_round_trip(self) -> None:
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
        self.assertEqual([], tools.json()["result"]["tools"])


if __name__ == "__main__":
    unittest.main()
