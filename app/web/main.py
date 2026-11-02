from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app import __version__
from app.config import BASE_DIR, settings
from app.db import init_db
from app.mcp.http import MCPAuthMiddleware, build_mcp_http_app
from app.security import check_basic_auth, session_secret
from app.web.routes import router

mcp_http_app = build_mcp_http_app()


class AdminAuthMiddleware:
    """Optional HTTP Basic auth for the admin UI. MCP keeps its own Key auth."""

    _SKIP_PREFIXES = ("/mcp", "/static")
    _SKIP_PATHS = {"/healthz"}

    def __init__(self, app: ASGIApp):
        self.app = app

    @staticmethod
    def _is_cross_origin(scope: Scope) -> bool:
        if scope.get("method", "").upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
            return False
        headers = Headers(scope=scope)
        source = headers.get("origin") or headers.get("referer")
        if not source:
            return False
        host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0]
        return urlparse(source).netloc != host.strip()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path in self._SKIP_PATHS or path.startswith(
            self._SKIP_PREFIXES
        ):
            await self.app(scope, receive, send)
            return

        if self._is_cross_origin(scope):
            await PlainTextResponse("拒绝跨站请求", status_code=403)(
                scope, receive, send
            )
            return

        if not settings.admin_password:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        authorized = False
        header = headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                username, _, password = decoded.partition(":")
                authorized = check_basic_auth(username, password)
            except (binascii.Error, UnicodeDecodeError, ValueError):
                authorized = False

        if not authorized:
            response = PlainTextResponse(
                "需要管理员账号密码",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="DatabaseAItool"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        yield


app = FastAPI(
    title=settings.app_name,
    version=__version__,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(SessionMiddleware, secret_key=session_secret(), same_site="lax")
app.add_middleware(AdminAuthMiddleware)

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "app" / "web" / "static")),
    name="static",
)
app.include_router(router)

# MCP Streamable HTTP endpoint lives at /mcp; mounted last so UI routes win.
app.mount("/", MCPAuthMiddleware(mcp_http_app))


def main() -> None:
    uvicorn.run(
        "app.web.main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
