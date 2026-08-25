"""Console entry point for the Dev Workflow Service transport (`personal-agent-dal-api`).

This is the offline-slice runner: it assembles the service composition root
(`create_app`) over an explicit workflow database and an owner-only service key
file, then serves uvicorn. Production deployment (systemd/Nginx/TLS/backup) is
out of scope here and is a separate `external-live` gate.
"""

from __future__ import annotations

import argparse
import stat
import sys
from pathlib import Path

import uvicorn

from personal_agent_dal.service.app import create_app
from personal_agent_dal.storage.engine import create_database_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent-dal-api")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--service-key-file", type=Path, required=True)
    parser.add_argument("--kill-switch-path", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if not args.database.exists():
        print("database missing: run the DAL migration first", file=sys.stderr)
        return 1

    try:
        mode = stat.S_IMODE(args.service_key_file.stat().st_mode)
    except OSError as error:
        print(f"service key file unreadable: {type(error).__name__}", file=sys.stderr)
        return 1
    if mode != 0o600:
        print("service key file must be owner-only (0600)", file=sys.stderr)
        return 1
    service_key = args.service_key_file.read_bytes().strip()

    engine = create_database_engine(args.database)
    try:
        app = create_app(
            engine,
            service_key=service_key,
            kill_switch_path=args.kill_switch_path,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
