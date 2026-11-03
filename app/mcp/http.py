from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.transport_security import TransportSecuritySettings

from app.config import settings
from app.db import SessionLocal
from app.mcp.auth import extract_raw_key, key_context
from app.mcp.server import mcp
from app.services.connections import authenticate_api_key, mark_key_used


def _allowed_hosts() -> list[str]:
    hosts: list[str] = []
    for item in settings.mcp_allowed_hosts.split(","):
        item = item.strip()
        if item:
            hosts.append(item)
    hosts += ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]
    if settings.host not in ("", "0.0.0.0", "::"):
        hosts += [settings.host, f"{settings.host}:*"]
    return list(dict.fromkeys(hosts))


def _transport_security() -> TransportSecuritySettings:
    if settings.mcp_disable_host_check:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts(),
    )


def build_mcp_http_app():
    return mcp.streamable_http_app(
        streamable_http_path="/mcp",
        host=settings.host,
        transport_security=_transport_security(),
    )


class MCPAuthMiddleware:
    """Reject unauthenticated MCP requests before they reach the MCP transport."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw_key = extract_raw_key(headers)
        key_id: int | None = None
        if raw_key:
            with SessionLocal() as session:
                key = authenticate_api_key(session, raw_key)
                if key is not None:
                    key_id = key.id
                    mark_key_used(session, key)

        if key_id is None:
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": "Unauthorized: missing or invalid API key",
                    },
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="DatabaseAItool"'},
            )
            await response(scope, receive, send)
            return

        with key_context(key_id):
            await self.app(scope, receive, send)
