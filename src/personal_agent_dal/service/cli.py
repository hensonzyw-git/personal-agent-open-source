"""Console entry point for the Dev Workflow Service transport (`personal-agent-dal-api`).

This is the offline-slice runner: it assembles the service composition root
(`create_app`) over an explicit workflow database and two owner-only secret
files — the HMAC service key and the enrollment secret that gates `/enroll` —
then serves uvicorn. Production deployment (systemd/Nginx/TLS/backup) is out of
scope here and is a separate `external-live` gate.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

import uvicorn

from personal_agent_dal.service.app import LEASE_TTL_SECONDS, MAX_ATTEMPTS, create_app
from personal_agent_dal.storage.engine import create_database_engine


def _read_secret_file(path: Path, label: str) -> bytes | None:
    """Read a secret file; None (with stderr) on any failure.

    Two shapes are accepted, both from the DEV-032 deployment convention:
    0600 owned by the service user itself, or 0640 owned by root with the
    service user as the sole group (root:<service>, so the service cannot
    rewrite its own configuration and no other user shares the group).
    Anything wider than that — or a group-readable file not owned by root —
    is refused, never trimmed to a weaker check.
    """
    try:
        info = path.stat()
        mode = stat.S_IMODE(info.st_mode)
    except OSError as error:
        print(f"{label} unreadable: {type(error).__name__}", file=sys.stderr)
        return None
    if info.st_uid == 0:
        owner_ok = mode == 0o640
    else:
        owner_ok = info.st_uid == os.getuid() and mode == 0o600
    if not owner_ok:
        print(
            f"{label} must be 0600 owned by the service user "
            f"or 0640 owned by root (got mode {mode:04o})",
            file=sys.stderr,
        )
        return None
    value = path.read_bytes().strip()
    if not value:
        print(f"{label} must be non-empty", file=sys.stderr)
        return None
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent-dal-api")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--service-key-file", type=Path, required=True)
    parser.add_argument("--enrollment-secret-file", type=Path, required=True)
    parser.add_argument("--kill-switch-path", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    # Lease TTL and attempt budget are ECS authority (a worker never proposes
    # either); reclamation of an expired lease happens on `/jobs/claim`.
    parser.add_argument("--lease-ttl-seconds", type=int, default=LEASE_TTL_SECONDS)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    # TLS is terminated here only for the same-machine acceptance; production
    # terminates at Nginx and passes plain HTTP over loopback.
    parser.add_argument("--ssl-certfile", type=Path, default=None)
    parser.add_argument("--ssl-keyfile", type=Path, default=None)
    args = parser.parse_args(argv)

    if (args.ssl_certfile is None) != (args.ssl_keyfile is None):
        print("--ssl-certfile and --ssl-keyfile must be given together", file=sys.stderr)
        return 1
    if args.lease_ttl_seconds <= 0 or args.max_attempts < 1:
        print("--lease-ttl-seconds and --max-attempts must be positive", file=sys.stderr)
        return 1

    if not args.database.exists():
        print("database missing: run the DAL migration first", file=sys.stderr)
        return 1

    service_key = _read_secret_file(args.service_key_file, "service key file")
    if service_key is None:
        return 1
    enrollment_secret = _read_secret_file(
        args.enrollment_secret_file, "enrollment secret file"
    )
    if enrollment_secret is None:
        return 1

    engine = create_database_engine(args.database)
    try:
        app = create_app(
            engine,
            service_key=service_key,
            enrollment_secret=enrollment_secret,
            kill_switch_path=args.kill_switch_path,
            lease_ttl_seconds=args.lease_ttl_seconds,
            max_attempts=args.max_attempts,
        )
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
            ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
            ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None,
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
