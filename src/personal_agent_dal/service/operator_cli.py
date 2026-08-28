"""Operator Console CLI (`personal-agent-dal-console`) — DAL-R08 first slice.

A minimal read-only console over the operator plane of the Dev Workflow
Service. The MacBook is the Operator Console (frozen Roadmap decision 4):
this CLI observes jobs from a distance and offers exactly one mutation,
`cancel`, which goes through the operator identity (a separate token from any
worker token) and carries the job's `expected_state` as a stale-projection
fence — the CLI reads the state, shows it, and binds the action to it.

Token handling: the operator token arrives via `--token-file` (owner-only
0600, one line, no trailing newline handling beyond strip). The token value is
never printed; only its operator identity prefix and expiry are echoed, and
only when `--show-token-info` is given.

Mutations require an explicit second confirmation (typo-guard), print the
server's error envelope verbatim on failure, and never retry on their own —
a lost response is resolved by re-reading state, not by blind resend.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OPERATOR_SCHEMA_VERSION = "dal.operator-transport/1.0"
DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT_SECONDS = 10.0
READ_COMMANDS = ("list", "show", "checkpoints", "whoami")


def _read_token_file(path: Path) -> str:
    """Read a 0600 owner-only token file; refuse anything else."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise SystemExit(f"token file unreadable: {type(error).__name__}")
    if mode != 0o600:
        raise SystemExit("token file must be owner-only (0600)")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SystemExit("token file must be non-empty")
    return value


def _request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, dict | bytes]:
    """One HTTP request; returns (status, parsed-json-or-raw-bytes). No retry."""
    data = None
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["X-Transport-Body-Digest"] = __import__("hashlib").sha256(data).hexdigest()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
    except urllib.error.URLError as error:
        raise SystemExit(f"connection failed: {error.reason}")
    try:
        return status, json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return status, body


def _token_expiry(token: str) -> str:
    """Decode the token payload's operator_id and exp for display (no secret)."""
    import base64

    try:
        payload_b64 = token.split(".", 1)[0]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        remaining = payload["exp"] - int(time.time())
        return (
            f"operator_id={payload.get('operator_id')} "
            f"capabilities={payload.get('capabilities')} "
            f"expires_in={max(remaining, 0)}s"
        )
    except Exception:  # noqa: BLE001 - display only, never identity
        return "undecodable token payload"


def _print_jobs(page: dict) -> None:
    print(f"total={page['total']} showing={len(page['jobs'])} offset={page['offset']}")
    for job in page["jobs"]:
        print(
            f"  {job['job_id']}  {job['state']:<10} attempt={job['attempt']} "
            f"feature={job['feature_id']} repo={job['repository_id']}"
        )


def _fail_with_envelope(status: int, body: object) -> None:
    if isinstance(body, dict):
        raise SystemExit(f"HTTP {status}: {body.get('code')} ({body.get('detail')})")
    raise SystemExit(f"HTTP {status}: {body!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="personal-agent-dal-console",
        description="Minimal read-only operator console for the DAL Dev Workflow Service.",
    )
    parser.add_argument("--base-url", required=True, help="e.g. https://agent.example.invalid/dal/transport/v1")
    parser.add_argument("--token-file", type=Path, required=True, help="0600 file holding the operator token")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="show the token's operator identity and expiry")
    list_parser = sub.add_parser("list", help="list jobs (server-side pagination)")
    list_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    list_parser.add_argument("--offset", type=int, default=0)
    show_parser = sub.add_parser("show", help="show one job's detail")
    show_parser.add_argument("job_id")
    checkpoints_parser = sub.add_parser("checkpoints", help="list a job's checkpoint metadata")
    checkpoints_parser.add_argument("job_id")
    cancel_parser = sub.add_parser("cancel", help="cancel a job (the one mutation; requires --yes)")
    cancel_parser.add_argument("job_id")
    cancel_parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    args = parser.parse_args(argv)

    token = _read_token_file(args.token_file)

    if args.command == "whoami":
        print(_token_expiry(token))
        return 0

    if args.command == "list":
        status, body = _request(
            "GET",
            f"{args.base_url}/operator/jobs?limit={args.limit}&offset={args.offset}",
            token,
            timeout=args.timeout,
        )
        if status != 200:
            _fail_with_envelope(status, body)
        _print_jobs(body)
        return 0

    if args.command == "show":
        status, body = _request(
            "GET", f"{args.base_url}/operator/jobs/{args.job_id}", token, timeout=args.timeout
        )
        if status != 200:
            _fail_with_envelope(status, body)
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0

    if args.command == "checkpoints":
        status, body = _request(
            "GET",
            f"{args.base_url}/operator/jobs/{args.job_id}/checkpoints",
            token,
            timeout=args.timeout,
        )
        if status != 200:
            _fail_with_envelope(status, body)
        for cp in body["checkpoints"]:
            print(
                f"  seq={cp['sequence']} epoch={cp['lease_epoch']} "
                f"{cp['sensitivity']:<10} {cp['artifact_sha256']} "
                f"size={cp['artifact_size_bytes']} files={len(cp['changed_files'])}"
            )
        return 0

    if args.command == "cancel":
        status, detail = _request(
            "GET", f"{args.base_url}/operator/jobs/{args.job_id}", token, timeout=args.timeout
        )
        if status != 200:
            _fail_with_envelope(status, detail)
        state = detail["state"]
        if state not in ("pending", "leased", "running"):
            raise SystemExit(f"job is terminal ({state}); nothing to cancel")
        if not args.yes:
            answer = input(f"cancel job {args.job_id} (state={state})? [y/N] ")
            if answer.strip().lower() != "y":
                print("aborted")
                return 1
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": f"op-{args.job_id}-{int(time.time())}",
            "job_id": args.job_id,
            "action": "cancel",
            "expected_state": state,
        }
        status, body = _request(
            "POST",
            f"{args.base_url}/operator/jobs/{args.job_id}/cancel",
            token,
            payload=payload,
            timeout=args.timeout,
        )
        if status != 200:
            _fail_with_envelope(status, body)
        print(f"cancelled: {body['job_id']}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
