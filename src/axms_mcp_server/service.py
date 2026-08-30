from __future__ import annotations

from collections.abc import Callable
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from axms_mcp_server import __version__
from axms_mcp_server.coding.tools import register_coding_tools
from axms_mcp_server.common.auth import BearerTokenMiddleware, validate_service_token
from axms_mcp_server.common.catalog import PRODUCTION_TOOL_NAMES, validate_known_catalog
from axms_mcp_server.config import Settings


SERVER_NAME = "urizo-final-mcp-server"
PROTOCOL_VERSION = "2026-07-28"


def create_server(settings: Settings | None = None) -> MCPServer[Any]:
    validate_known_catalog()
    active_settings = settings or Settings()
    server: MCPServer[Any] = MCPServer(
        name=SERVER_NAME,
        title="AX Module Studio MCP Server",
        description="Shared authenticated MCP boundary for Coding and Natural CMS tools.",
        instructions="Only tools explicitly registered by an approved feature may be called.",
        version=__version__,
    )
    registered_tool_names = register_coding_tools(
        server,
        active_settings.workspace_root,
        active_settings.git_executable,
    )
    if registered_tool_names != PRODUCTION_TOOL_NAMES:
        raise ValueError("Production MCP registration does not match the approved catalog.")

    @server.custom_route("/health/live", methods=["GET"], include_in_schema=False)
    async def live(_: Request) -> Response:
        return JSONResponse({"status": "UP", "service": SERVER_NAME})

    @server.custom_route("/health/ready", methods=["GET"], include_in_schema=False)
    async def ready(_: Request) -> Response:
        return JSONResponse(
            {
                "status": "READY",
                "service": SERVER_NAME,
                "protocolVersion": PROTOCOL_VERSION,
                "registeredToolCount": len(registered_tool_names),
            }
        )

    return server


def create_application(settings: Settings, service_token: str) -> ASGIApp:
    validate_service_token(service_token)
    server = create_server(settings)
    application = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=settings.max_request_body_size,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        ),
        host=settings.host,
    )
    return BearerTokenMiddleware(application, service_token, protected_path="/mcp")


def application_factory() -> ASGIApp:
    settings = Settings.from_environment()
    return create_application(settings, settings.read_service_token())


def main(run: Callable[..., None] = uvicorn.run) -> None:
    settings = Settings.from_environment()
    application = create_application(settings, settings.read_service_token())
    run(application, host=settings.host, port=settings.port, log_level=settings.log_level)


if __name__ == "__main__":
    main()
