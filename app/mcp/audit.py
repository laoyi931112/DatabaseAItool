from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

from app.adapters.base import AdapterError, safe_error
from app.services import operation_logs
from app.services.connections import ServiceError

logger = logging.getLogger(__name__)

# 长时间运行的进程也要按保留期清理，但不必每次写日志都扫一遍表。
PRUNE_INTERVAL_SECONDS = 3600.0
_last_prune = 0.0


def _maybe_prune() -> None:
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < PRUNE_INTERVAL_SECONDS:
        return
    _last_prune = now
    try:
        operation_logs.prune_expired()
    except Exception:
        logger.exception("清理过期操作日志失败")


def _write(
    signature: inspect.Signature,
    operation: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    status: str,
    error_message: str | None,
    row_count: int | None,
    duration_ms: int,
) -> None:
    try:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = dict(bound.arguments)
        key_id = values.pop("key_id", None)
        database_name = values.pop("database", None)
        operation_logs.record(
            key_id=key_id,
            database_name=str(database_name) if database_name else None,
            operation=operation,
            detail=operation_logs.build_detail(values),
            status=status,
            error_message=(
                error_message[: operation_logs.MAX_ERROR_MESSAGE]
                if error_message
                else None
            ),
            duration_ms=duration_ms,
            row_count=row_count,
        )
        _maybe_prune()
    except Exception:
        # 日志永远不能影响工具本身。
        logger.exception("写入操作日志失败")


def audit_log(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """记录一次 MCP 数据访问：调用者、目标库、入参摘要、耗时与结果。"""
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        status = operation_logs.STATUS_SUCCESS
        error_message: str | None = None
        row_count: int | None = None
        try:
            result = fn(*args, **kwargs)
            if isinstance(result, dict):
                count = result.get("row_count")
                row_count = int(count) if isinstance(count, (int, float)) else None
            return result
        except (AdapterError, PermissionError, ServiceError) as exc:
            status, error_message = operation_logs.STATUS_ERROR, str(exc)
            raise
        except Exception as exc:
            status, error_message = operation_logs.STATUS_ERROR, safe_error(exc)
            raise
        finally:
            _write(
                signature,
                fn.__name__,
                args,
                kwargs,
                status,
                error_message,
                row_count,
                int((time.perf_counter() - started) * 1000),
            )

    return wrapper
