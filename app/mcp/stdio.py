from __future__ import annotations

import sys

from app.db import init_db
from app.mcp.auth import AuthenticationError, initialize_stdio_key
from app.mcp.server import mcp


def main() -> None:
    init_db()
    try:
        initialize_stdio_key()
    except AuthenticationError as exc:
        print(f"DatabaseAItool: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
