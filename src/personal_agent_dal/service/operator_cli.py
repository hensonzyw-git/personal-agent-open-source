"""Operator Console CLI (`personal-agent-dal-console`) — DAL-R08 first slice.

A minimal console over the operator plane of the Dev Workflow Service: the
read commands `list`/`show`/`checkpoints`/`whoami`, the `cancel` mutation,
and (R09-B F5) the two GitHub dispatch-executor commands. The MacBook is the
Operator Console (frozen Roadmap decision 4):
this CLI observes jobs from a distance and cancels through the operator
identity (a separate token from any worker token), carrying the job's
`expected_state` as a stale-projection fence — the CLI reads the state, shows
it, and binds the action to it.

F5 executor commands, both carrying the same state/version fence:

- `effects` — list the effects currently in `unknown` (the reconciliation
  sweep's backlog).
- `wake` — approve/wake one persisted effect. The operator names only the
  effect and the state/version they believe it holds; every field the
  outward write needs (owner, action, payload, remote idempotency key) is
  derived server-side from persistence. A second confirmation is required,
  like `cancel`.
- `reconcile-sweep` — one read-only reconciliation pass over the unknown
  effects. This is also the persistent-task driver's entry point (the
  systemd timer calls exactly this); it issues no writes.

Token handling: interactive commands use `--token-file` (owner-only 0600).
The server-local systemd sweep may instead use `--service-key-file`; that mode
is structurally restricted to `reconcile-sweep` and mints a two-minute control
token in memory.  No bearer token is written to disk or printed.

Mutations require an explicit second confirmation (typo-guard). Failures
print the server's error envelope fields (code, detail) and never a
traceback; requests are never retried or re-sent on redirect — an HTTP
redirect is refused instead of followed, so the bearer token never leaves
the configured origin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

OPERATOR_SCHEMA_VERSION = "dal.operator-transport/1.0"
DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT_SECONDS = 10.0
SERVER_LOCAL_SWEEP_BASE_URL = "http://127.0.0.1:8820"
READ_COMMANDS = ("list", "show", "checkpoints", "whoami", "effects")


def _read_token_file(path: Path) -> str:
    """Read a 0600 owner-only regular token file, race-free.

    The fd is opened with `O_NOFOLLOW` and the ownership/mode checks run on
    that same fd (`fstat`), so the file verified is exactly the file read —
    no stat/read TOCTOU window, no symlink swap.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise SystemExit(f"token file unreadable: {type(error).__name__}") from None
    try:
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SystemExit("token file must be a regular file")
        if info.st_uid != os.getuid():
            raise SystemExit("token file must be owned by the current user")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise SystemExit("token file must be owner-only (0600)")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as error:
        raise SystemExit(f"token file unreadable: {type(error).__name__}") from None
    finally:
        os.close(fd)
    value = b"".join(chunks).decode("utf-8", errors="strict").strip()
    if not value:
        raise SystemExit("token file must be non-empty")
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it.

    The default handler would re-send the Authorization header to whatever
    host the redirect names; for a credential-bearing operator client that is
    a credential leak by design. A redirect response therefore surfaces as a
    normal HTTPError status for `_fail_with_envelope`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
#: Path to a private CA bundle the operator explicitly trusts, set once by
#: `main` from `--ca-bundle`. Never a downgrade: verification is always on,
#: this only swaps which anchors verify the server certificate.
_CA_BUNDLE: Path | None = None


def _opener() -> urllib.request.OpenerDirector:
    """The no-redirect opener, rebuilt when a private CA bundle is set."""
    if _CA_BUNDLE is None:
        return _OPENER
    import ssl

    context = ssl.create_default_context(cafile=str(_CA_BUNDLE))
    return urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=context)
    )


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
        headers["X-Transport-Body-Digest"] = hashlib.sha256(data).hexdigest()
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
    except urllib.error.URLError as error:
        reason = getattr(error, "reason", error)
        raise SystemExit(f"connection failed: {type(reason).__name__}") from None
    except (TimeoutError, OSError) as error:
        raise SystemExit(f"connection failed: {type(error).__name__}") from None
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
    for key in ("total", "jobs", "offset"):
        if key not in page or not isinstance(page[key], (int, list)):
            raise SystemExit(f"malformed success response: missing {key!r}")
    print(f"total={page['total']} showing={len(page['jobs'])} offset={page['offset']}")
    for job in page["jobs"]:
        print(
            f"  {job['job_id']}  {job['state']:<10} attempt={job['attempt']} "
            f"feature={job['feature_id']} repo={job['repository_id']}"
        )


def _fail_with_envelope(status: int, body: object) -> None:
    if isinstance(body, dict):
        # The full envelope minus non-display fields — schema_version included,
        # so what the server said is what the operator sees.
        printable = {k: v for k, v in body.items() if k != "request_id"}
        raise SystemExit(f"HTTP {status}: {json.dumps(printable, ensure_ascii=False)}")
    raise SystemExit(f"HTTP {status}: {body!r}")


def _require_dict(body: object) -> dict:
    """A 2xx body that is not a JSON object is a protocol error, fail closed."""
    if not isinstance(body, dict):
        raise SystemExit("malformed success response: expected a JSON object")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="personal-agent-dal-console",
        description="Minimal read-only operator console for the DAL Dev Workflow Service.",
    )
    parser.add_argument("--base-url", required=True, help="e.g. https://agent.example.invalid/dal/transport/v1")
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument(
        "--token-file", type=Path, help="0600 file holding the operator token"
    )
    credentials.add_argument(
        "--service-key-file",
        type=Path,
        help="server-local reconcile-sweep only; mint an in-memory short token",
    )
    parser.add_argument(
        "--ca-bundle", type=Path, default=None,
        help="PEM bundle of a private CA to verify the server certificate "
        "(verification is always on; without this the system trust store is used)",
    )
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
    # F5: the durable GitHub dispatch executor's operator surface.
    effects_parser = sub.add_parser(
        "effects", help="list the effects in unknown (the reconciliation backlog)"
    )
    effects_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    wake_parser = sub.add_parser(
        "wake", help="approve/wake one persisted effect (requires --yes)"
    )
    wake_parser.add_argument("effect_id")
    wake_parser.add_argument(
        "--expected-state", required=True,
        help="the state the operator believes the effect holds",
    )
    wake_parser.add_argument(
        "--expected-version", type=int, required=True,
        help="the version the operator believes the effect holds",
    )
    wake_parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    sub.add_parser(
        "reconcile-sweep",
        help="run one read-only reconciliation pass over unknown effects",
    )
    args = parser.parse_args(argv)

    global _CA_BUNDLE
    _CA_BUNDLE = args.ca_bundle
    if args.service_key_file is not None:
        if args.command != "reconcile-sweep":
            parser.error("--service-key-file is only valid with reconcile-sweep")
        if args.base_url.rstrip("/") != SERVER_LOCAL_SWEEP_BASE_URL:
            parser.error(
                "--service-key-file requires the pinned loopback base URL "
                f"{SERVER_LOCAL_SWEEP_BASE_URL}"
            )
        from personal_agent_dal.service.cli import _read_secret_file
        from personal_agent_dal.service.operator_tokens import issue_operator_token

        service_key = _read_secret_file(args.service_key_file, "service key file")
        if service_key is None:
            return 1
        token = issue_operator_token(
            operator_id="dal-reconcile-timer",
            capabilities=["control"],
            expires_at_epoch=int(time.time()) + 120,
            key=service_key,
        )
    else:
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
        _print_jobs(_require_dict(body))
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
        page = _require_dict(body)
        for cp in page.get("checkpoints", []):
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
        state = _require_dict(detail).get("state")
        if not isinstance(state, str):
            raise SystemExit("malformed success response: missing 'state'")
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
        cancelled = _require_dict(body)
        print(f"cancelled: {cancelled.get('job_id', '?')}")
        return 0

    if args.command == "effects":
        status, body = _request(
            "GET", f"{args.base_url}/operator/effects?limit={args.limit}", token,
            timeout=args.timeout,
        )
        if status != 200:
            _fail_with_envelope(status, body)
        page = _require_dict(body)
        effects = page.get("effects")
        if not isinstance(effects, list):
            raise SystemExit("malformed success response: missing 'effects'")
        print(f"unknown effects: {len(effects)}")
        for effect in effects:
            print(
                f"  {effect['effect_id']}  v{effect['version']} "
                f"feature={effect['owner_aggregate_id']} "
                f"key={effect['remote_idempotency_key']}"
            )
        return 0

    if args.command == "wake":
        if not args.yes:
            answer = input(
                f"wake effect {args.effect_id} "
                f"(bound {args.expected_state}@v{args.expected_version})? [y/N] "
            )
            if answer.strip().lower() != "y":
                print("aborted")
                return 1
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": f"op-wake-{args.effect_id}-{int(time.time())}",
            "effect_id": args.effect_id,
            "expected_state": args.expected_state,
            "expected_version": args.expected_version,
        }
        status, body = _request(
            "POST", f"{args.base_url}/operator/effects/{args.effect_id}/wake",
            token, payload=payload, timeout=args.timeout,
        )
        if status != 200:
            _fail_with_envelope(status, body)
        woken = _require_dict(body)
        refusal = woken.get("refusal")
        if refusal is not None:
            print(f"refused: {refusal.get('code')} — {refusal.get('detail')}")
            return 1
        line = f"woken: {woken.get('effect_id')} -> {woken.get('effect_state')}"
        if woken.get("authoritative_result") is not None:
            line += f" authoritative={woken['authoritative_result']}"
        print(line)
        return 0

    if args.command == "reconcile-sweep":
        payload = {
            "schema_version": OPERATOR_SCHEMA_VERSION,
            "request_id": f"op-sweep-{int(time.time())}",
        }
        status, body = _request(
            "POST", f"{args.base_url}/operator/effects/reconcile-sweep",
            token, payload=payload, timeout=args.timeout,
        )
        if status != 200:
            _fail_with_envelope(status, body)
        swept = _require_dict(body).get("swept")
        if not isinstance(swept, list):
            raise SystemExit("malformed success response: missing 'swept'")
        print(f"swept: {len(swept)}")
        for item in swept:
            line = f"  {item['effect_id']} -> {item['effect_state']}"
            if item.get("authoritative_result") is not None:
                line += f" authoritative={item['authoritative_result']}"
            if item.get("refusal_code") is not None:
                line += f" refusal={item['refusal_code']}"
            print(line)
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
