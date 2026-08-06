"""Thin CLI entry point: `xgen_edit2docs serve` runs the FastAPI app via uvicorn.

Real subcommands (job admin, migration helpers) land in M3 / M6.
"""

from __future__ import annotations

import argparse
import sys

from .config import get_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xgen_edit2docs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Run the FastAPI server")
    serve.add_argument("--host", default=None, help="Override EDIT2DOCS_HOST")
    serve.add_argument("--port", type=int, default=None, help="Override EDIT2DOCS_PORT")
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)

    parser.error(f"unknown command: {args.command}")
    return 2


def _serve(args: argparse.Namespace) -> int:
    import uvicorn  # imported lazily so non-serve commands stay fast

    settings = get_settings()
    uvicorn.run(
        "xgen_edit2docs.api.main:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload or settings.debug,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
