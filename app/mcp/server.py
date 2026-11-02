from __future__ import annotations

from functools import partial
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import __version__
from app.adapters.base import AdapterError
from app.mcp import service
from app.mcp.auth import require_key_id
from app.services.connections import ServiceError

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

mcp = MCPServer(
    name="DatabaseAItool",
    title="DatabaseAItool - Read-only database access",
    version=__version__,
    instructions=(
        "This server provides strictly read-only access to databases. Each API key can "
        "only access the database connections assigned to it. Start with list_databases, "
        "then list_schemas/list_tables, describe_table, and finally query or sample_rows. "
        "Only a single SELECT statement is allowed for SQL databases. INSERT/UPDATE/DELETE, "
        "DDL, multi-statement SQL and administrative operations are rejected. "
        "For MongoDB use find_documents or aggregate_documents; $out and $merge are rejected."
    ),
)


async def _execute(
    ctx: Context, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
) -> dict[str, Any]:
    try:
        key_id = require_key_id(ctx.headers)
        return await anyio.to_thread.run_sync(partial(fn, key_id, *args, **kwargs))
    except (AdapterError, PermissionError, ServiceError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
async def list_databases(ctx: Context) -> dict[str, Any]:
    """List the database connections this API key is allowed to access.

    Returns each database's `name`, type, description and row limit. Use the
    `name` value as the `database` argument of the other tools.
    """
    return await _execute(ctx, service.list_databases)


@mcp.tool(annotations=READ_ONLY)
async def get_database_info(ctx: Context, database: str) -> dict[str, Any]:
    """Get basic information about an allowed database.

    Includes database type, server version and default schema. `database` must
    be one of the names returned by list_databases.
    """
    return await _execute(ctx, service.get_database_info, database)


@mcp.tool(annotations=READ_ONLY)
async def list_schemas(ctx: Context, database: str) -> dict[str, Any]:
    """List schemas (MySQL: databases, MongoDB: the configured database)."""
    return await _execute(ctx, service.list_schemas, database)


@mcp.tool(annotations=READ_ONLY)
async def list_tables(
    ctx: Context, database: str, schema: str | None = None
) -> dict[str, Any]:
    """List tables, views or MongoDB collections in an allowed database.

    Pass `schema` when the database type uses schemas (PostgreSQL, SQL Server,
    Oracle). For MySQL and MongoDB the configured database is used automatically.
    """
    return await _execute(ctx, service.list_tables, database, schema)


@mcp.tool(annotations=READ_ONLY)
async def describe_table(
    ctx: Context,
    database: str,
    table: str,
    schema: str | None = None,
) -> dict[str, Any]:
    """Describe a table/view/collection: columns, types, primary key, foreign keys and indexes.

    For MongoDB, fields are inferred from a sample of up to 50 documents.
    """
    return await _execute(ctx, service.describe_table, database, table, schema)


@mcp.tool(annotations=READ_ONLY)
async def query(
    ctx: Context,
    database: str,
    sql: str,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Execute one read-only SELECT statement against an allowed SQL database.

    Only SELECT (including CTEs and UNION/INTERSECT/EXCEPT) is accepted. Writes,
    DDL, multiple statements, SELECT INTO, locking reads and dangerous functions
    are rejected before execution. Rows are capped by max_rows and the database
    connection's configured limit; `truncated` indicates more rows existed.
    SQL is not supported for MongoDB.
    """
    return await _execute(ctx, service.query, database, sql, max_rows)


@mcp.tool(annotations=READ_ONLY)
async def sample_rows(
    ctx: Context,
    database: str,
    table: str,
    schema: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Read a small sample of rows from a table (or documents from a collection)."""
    return await _execute(ctx, service.sample_rows, database, table, schema, limit)


@mcp.tool(annotations=READ_ONLY)
async def find_documents(
    ctx: Context,
    database: str,
    collection: str,
    filter: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
    sort: dict[str, Any] | list[Any] | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    """Query MongoDB documents with a read-only find.

    `filter`, `projection` and `sort` use MongoDB JSON syntax. Only the database
    assigned to the connection is accessible.
    """
    return await _execute(
        ctx,
        service.find_documents,
        database,
        collection,
        filter,
        projection,
        sort,
        limit,
    )


@mcp.tool(annotations=READ_ONLY)
async def aggregate_documents(
    ctx: Context,
    database: str,
    collection: str,
    pipeline: list[dict[str, Any]],
    limit: int | None = 50,
) -> dict[str, Any]:
    """Run a read-only MongoDB aggregation pipeline.

    The $out and $merge stages are rejected because they write data. The result
    is capped by `limit` and the connection's configured row limit.
    """
    return await _execute(
        ctx, service.aggregate_documents, database, collection, pipeline, limit
    )
