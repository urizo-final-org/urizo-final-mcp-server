# AX Module Studio MCP Server

Single Python MCP runtime for the shared AX Module Studio tool boundary. The service exposes one Streamable HTTP endpoint at `/mcp`, keeps the production tool catalog empty during AI06-010, and reserves the approved Coding/CMS tool names without registering placeholder tools.

## Runtime boundary

- Spring Backend is the only platform client and owns authorization, job state, profile/version binding, and Core persistence.
- This repository owns the MCP transport, service-token authentication, fixed tool-name catalog, health endpoints, tests, and image.
- The service has no PostgreSQL, Valkey, checkpoint, or Core database dependency.
- `/health/live` and `/health/ready` are public container health endpoints. `/mcp` requires `Authorization: Bearer <service-token>`.

## Local verification

Python `3.12.13` and `uv 0.8.13` are the repository baseline.

```powershell
uv sync --frozen
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src tests
docker build -t axms/mcp-server:dev .
```

Run the server only with a token file; do not put token values on the command line or in logs.

```powershell
$env:AXMS_MCP_SERVICE_TOKEN_FILE = 'C:\path\to\mcp_service_token'
uv run python -m axms_mcp_server.service
```

Default port: `8091`.
