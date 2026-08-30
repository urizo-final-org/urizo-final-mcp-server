from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from axms_mcp_server.common.auth import validate_service_token


DEFAULT_TOKEN_FILE = Path("/run/secrets/mcp_service_token")
DEFAULT_ALLOWED_HOSTS = (
    "mcp-server",
    "mcp-server:*",
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
)
ALLOWED_LOG_LEVELS = frozenset({"critical", "error", "warning", "info", "debug"})


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _integer(environment: dict[str, str], name: str, default: int) -> int:
    raw = environment.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exception:
        raise ValueError(f"{name} must be an integer.") from exception


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "0.0.0.0"
    port: int = 8091
    service_token_file: Path = DEFAULT_TOKEN_FILE
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    allowed_origins: tuple[str, ...] = ()
    max_request_body_size: int = 65_536
    log_level: str = "info"

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("AXMS_MCP_HOST must not be blank.")
        if self.port < 1 or self.port > 65_535:
            raise ValueError("AXMS_MCP_PORT must be between 1 and 65535.")
        if not self.allowed_hosts:
            raise ValueError("AXMS_MCP_ALLOWED_HOSTS must contain at least one host.")
        if self.max_request_body_size < 1 or self.max_request_body_size > 1_048_576:
            raise ValueError("AXMS_MCP_MAX_REQUEST_BODY_SIZE must be between 1 and 1048576.")
        if self.log_level.lower() not in ALLOWED_LOG_LEVELS:
            raise ValueError("AXMS_MCP_LOG_LEVEL is invalid.")

    @classmethod
    def from_environment(cls, environment: dict[str, str] | None = None) -> Settings:
        values = os.environ if environment is None else environment
        default_hosts = ",".join(DEFAULT_ALLOWED_HOSTS)
        return cls(
            host=values.get("AXMS_MCP_HOST", "0.0.0.0"),
            port=_integer(values, "AXMS_MCP_PORT", 8091),
            service_token_file=Path(values.get("AXMS_MCP_SERVICE_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))),
            allowed_hosts=_csv(values.get("AXMS_MCP_ALLOWED_HOSTS", default_hosts)),
            allowed_origins=_csv(values.get("AXMS_MCP_ALLOWED_ORIGINS", "")),
            max_request_body_size=_integer(values, "AXMS_MCP_MAX_REQUEST_BODY_SIZE", 65_536),
            log_level=values.get("AXMS_MCP_LOG_LEVEL", "info").lower(),
        )

    def read_service_token(self) -> str:
        try:
            token = self.service_token_file.read_text(encoding="utf-8").strip()
        except OSError as exception:
            raise RuntimeError("MCP service token file could not be read.") from exception
        validate_service_token(token)
        return token
