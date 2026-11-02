from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Mapping

from app.db import SessionLocal
from app.services.connections import authenticate_api_key, mark_key_used

_current_key_id: ContextVar[int | None] = ContextVar("mcp_key_id", default=None)
_stdio_key_id: int | None = None


class AuthenticationError(PermissionError):
    pass


def extract_raw_key(headers: Mapping[str, str] | None) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        lowered = key.lower()
        if lowered == "authorization":
            scheme, _, token = value.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
        elif lowered == "x-api-key":
            if value.strip():
                return value.strip()
    return None


def initialize_stdio_key() -> int:
    """Authenticate MCP_API_KEY once for the stdio transport."""
    global _stdio_key_id
    raw_key = (os.environ.get("MCP_API_KEY") or "").strip()
    if not raw_key:
        raise AuthenticationError(
            "stdio 模式需要在环境变量 MCP_API_KEY 中提供 API Key"
        )
    with SessionLocal() as session:
        key = authenticate_api_key(session, raw_key)
        if key is None:
            raise AuthenticationError("API Key 无效、已禁用或已过期")
        _stdio_key_id = key.id
        mark_key_used(session, key)
        return key.id


def require_key_id(headers: Mapping[str, str] | None = None) -> int:
    key_id = _current_key_id.get() or _stdio_key_id
    if key_id is not None:
        return key_id

    raw_key = extract_raw_key(headers)
    if not raw_key:
        raise AuthenticationError("缺少 Authorization: Bearer <API Key> 或 X-API-Key")

    with SessionLocal() as session:
        key = authenticate_api_key(session, raw_key)
        if key is None:
            raise AuthenticationError("API Key 无效、已禁用或已过期")
        mark_key_used(session, key)
        return key.id


@contextmanager
def key_context(key_id: int) -> Iterator[None]:
    token = _current_key_id.set(key_id)
    try:
        yield
    finally:
        _current_key_id.reset(token)
