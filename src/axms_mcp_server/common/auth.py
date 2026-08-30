from __future__ import annotations

import hmac

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def validate_service_token(token: str) -> None:
    if len(token) < 43 or len(token) > 512 or any(character.isspace() for character in token):
        raise ValueError("MCP service token must be an opaque 43-512 character value without whitespace.")


class BearerTokenMiddleware:
    def __init__(self, application: ASGIApp, service_token: str, *, protected_path: str) -> None:
        validate_service_token(service_token)
        self._application = application
        self._service_token = service_token
        self._protected_path = protected_path.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_protected(scope.get("path", "")):
            await self._application(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        authorized = (
            separator == " "
            and scheme.lower() == "bearer"
            and bool(supplied_token)
            and hmac.compare_digest(supplied_token, self._service_token)
        )
        if not authorized:
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        await self._application(scope, receive, send)

    def _is_protected(self, path: str) -> bool:
        return path == self._protected_path or path.startswith(self._protected_path + "/")
