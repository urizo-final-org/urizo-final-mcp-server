# AX Module Studio MCP Server

Single Python MCP runtime for the shared AX Module Studio tool boundary. The service exposes one Streamable HTTP endpoint at `/mcp` and registers the seven approved Coding tools while keeping the reserved CMS tools disabled.

## Runtime boundary

- Spring Backend is the only platform client and owns authorization, job state, profile/version binding, and Core persistence.
- This repository owns the MCP transport, service-token authentication, fixed tool-name catalog, health endpoints, tests, and image.
- The service has no PostgreSQL, Valkey, checkpoint, or Core database dependency.
- `/health/live` and `/health/ready` are public container health endpoints. `/mcp` requires `Authorization: Bearer <service-token>`.
- Coding tools accept an opaque direct-child workspace key beneath `AXMS_MCP_WORKSPACE_ROOT` (default `/workspaces`). Absolute paths, linked paths, authentication/security code, secrets, Git metadata, and Flyway/migration paths are denied.
- The service does not clone or create workspaces. Production must mount pre-provisioned direct-child Git workspaces with read/write access for UID `10001`; that provisioning path remains outside AI04-002.
- Fixed Git operations use only the startup-validated absolute `AXMS_MCP_GIT_EXECUTABLE` (default `/usr/bin/git`); tool input cannot select an executable or command.
- `apply_patch` is serialized per workspace and fully validated against a temporary Git index before mutation. Each patch is limited to 500 unique paths and may change only stage-zero regular `100644` files.
- `run_check` accepts only `git-diff-check` and the import-free `python-syntax` AST profile. It never accepts a command, argument list, environment, or shell fragment.

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
