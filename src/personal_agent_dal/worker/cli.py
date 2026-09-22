"""Command-line entry point for the Home Mac Worker.

Legacy poll-once remains available. workflow-serve owns a serial Timeline
scheduler with durable recovery and a local single-instance lock.
"""

from __future__ import annotations

import argparse
import stat
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text

from personal_agent_dal.storage.db import alembic_config
from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker.config import (
    LocalTransportConfig,
    RemoteTransportConfig,
    WorkerConfig,
    load_worker_config,
)
from personal_agent_dal.worker.poll_once import run_poll_once
from personal_agent_dal.worker.remote import (
    EndpointError,
    RemoteHttpAdapter,
    RemoteTransportSettings,
    read_token_expiry,
    validate_endpoint,
)
from personal_agent_dal.worker.transport import LocalSQLiteAdapter, WorkerTransport

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent-dal-worker")
    parser.add_argument(
        "--config", type=Path, required=True, help="path to the worker config JSON"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("poll-once", help="claim one job, run it, record the result, exit")
    workflow = sub.add_parser("workflow-poll-once", help="execute one admitted Timeline v3 attempt")
    workflow.add_argument("--workflow-config", type=Path, required=True)
    daemon = sub.add_parser("workflow-serve", help="run the serial Timeline workflow scheduler")
    daemon.add_argument("--workflow-config", type=Path, required=True)
    sub.add_parser("healthcheck", help="verify config, database and repos are reachable")
    args = parser.parse_args(argv)

    try:
        config = load_worker_config(args.config)
    except (OSError, ValueError) as error:
        print(f"config error: {type(error).__name__}", file=sys.stderr)
        return 1

    if args.command == "healthcheck":
        return _healthcheck(config)
    if args.command == "workflow-serve":
        from personal_agent_dal.worker.workflow_daemon import serve
        return serve(config,args.workflow_config)
    if args.command == "workflow-poll-once":
        from personal_agent_dal.worker.workflow import WorkflowWorker,load_config
        try:
            with _open_transport(config) as transport:
                if not isinstance(transport,RemoteHttpAdapter):raise ValueError('REMOTE_WORKFLOW_TRANSPORT_REQUIRED')
                result=WorkflowWorker(transport,load_config(args.workflow_config)).poll()
                return 0 if result.get('status') in ('idle','completed') else 1
        except Exception as error:
            print(f"workflow worker refused: {type(error).__name__}",file=sys.stderr)
            return 1
    return _poll_once(config)


@contextmanager
def _open_transport(config: WorkerConfig) -> Iterator[WorkerTransport]:
    """Build the transport this config declares, and only that one.

    In remote mode nothing here can open a database: there is no path to open,
    and `create_database_engine` is not reachable from this branch.
    """
    transport_config = config.transport
    if isinstance(transport_config, LocalTransportConfig):
        engine = create_database_engine(transport_config.database_path)
        try:
            yield LocalSQLiteAdapter(
                engine,
                worker_id=config.worker_id,
                lease_ttl_seconds=config.lease_ttl_seconds,
                max_attempts=config.max_attempts,
                checkpoint_root=config.checkpoint_root,
            )
        finally:
            engine.dispose()
        return
    from personal_agent_dal.worker.prelaunch import transport_identity
    transport = RemoteHttpAdapter(
        RemoteTransportSettings(
            identity=transport_identity(config),
            endpoint=transport_config.endpoint,
            worker_id=config.worker_id,
            machine_id=transport_config.machine_id,
            capabilities=transport_config.capabilities,
            enrollment_secret_path=transport_config.enrollment_secret_path,
            token_cache_path=transport_config.token_cache_path,
            checkpoint_root=config.checkpoint_root,
            ca_bundle_path=transport_config.ca_bundle_path,
            request_timeout_seconds=transport_config.request_timeout_seconds,
            retry_attempts=transport_config.retry_attempts,
            backoff_base_seconds=transport_config.backoff_base_seconds,
            backoff_max_seconds=transport_config.backoff_max_seconds,
        )
    )
    try:
        yield transport
    finally:
        transport.close()


def _poll_once(config: WorkerConfig) -> int:
    if isinstance(config.transport, LocalTransportConfig) and (
        not config.transport.database_path.exists()
    ):
        print("database missing: run the DAL migration first", file=sys.stderr)
        return 1
    try:
        with _open_transport(config) as transport:
            outcome = run_poll_once(transport, config)
    except Exception as error:  # noqa: BLE001 - fail closed, bounded message
        print(f"worker error: {type(error).__name__}", file=sys.stderr)
        return 1
    if not outcome.claimed and outcome.error is not None:
        print(f"worker disabled: {outcome.error}", file=sys.stderr)
        return 1
    if not outcome.claimed:
        return 0
    if outcome.state == "succeeded":
        return 0
    if outcome.error is not None:
        print(f"job {outcome.job_id}: {outcome.error}", file=sys.stderr)
    return 1


def _healthcheck(config: WorkerConfig) -> int:
    problems: list[str] = []
    transport = config.transport
    if isinstance(transport, LocalTransportConfig):
        problems.extend(_local_problems(transport))
    else:
        problems.extend(_remote_problems(transport))
    if config.kill_switch_path.exists():
        problems.append("kill_switch active")
    for repository_id, entry in config.repos.items():
        repo_path = Path(entry.local_path)
        if not repo_path.is_dir():
            problems.append(f"repo {repository_id} local_path missing")
        elif not (repo_path / ".git").exists():
            problems.append(f"repo {repository_id} is not a git checkout")
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1
    print("ok")
    return 0


def _local_problems(transport: LocalTransportConfig) -> list[str]:
    """Local mode: require the bundled migration heads without migrating."""
    if not transport.database_path.exists():
        return ["database_path missing"]
    try:
        engine = create_database_engine(transport.database_path)
        try:
            with engine.connect() as connection:
                revisions = set(connection.scalars(
                    text("SELECT version_num FROM alembic_version")
                ))
            heads = set(ScriptDirectory.from_config(alembic_config(engine)).get_heads())
            if revisions != heads:
                return ["database schema is not at current migration heads"]
        finally:
            engine.dispose()
    except Exception:  # noqa: BLE001 - healthcheck reports a bounded category
        return ["database schema unreadable"]
    return []


def _remote_problems(transport: RemoteTransportConfig) -> list[str]:
    """Remote mode: prove the endpoint is pinned and a credential is usable.

    Nothing here reads a credential's content and nothing here reaches the
    network. The enrollment secret is checked by file mode only; the token is
    checked through the cache's non-secret `expires_at`, so a healthcheck can
    say "a usable token is present" without ever handling the token itself.
    """
    problems: list[str] = []
    try:
        pinned = validate_endpoint(transport.endpoint)
    except EndpointError as error:
        return [f"endpoint not pinnable: {error}"]
    if pinned != transport.endpoint:
        problems.append("endpoint is not in pinned normal form")
    if transport.ca_bundle_path is not None and not transport.ca_bundle_path.is_file():
        problems.append("ca_bundle_path missing")

    try:
        mode = stat.S_IMODE(transport.enrollment_secret_path.stat().st_mode)
    except OSError:
        problems.append("enrollment secret missing")
    else:
        if mode != 0o600:
            problems.append("enrollment secret must be owner-only (0600)")

    if not transport.token_cache_path.exists():
        # Not a problem: the first poll enrolls. Say so rather than fail.
        print("token cache absent: the next poll will enroll")
        return problems
    try:
        token_mode = stat.S_IMODE(transport.token_cache_path.stat().st_mode)
    except OSError:
        problems.append("token cache unreadable")
        return problems
    if token_mode != 0o600:
        problems.append("token cache must be owner-only (0600)")
    expiry = read_token_expiry(transport.token_cache_path)
    if expiry is None:
        problems.append("token cache is not a readable token cache")
        return problems
    worker_id, expires_at = expiry
    remaining = expires_at - int(time.time())
    if remaining <= 0:
        print(f"token for {worker_id} expired: the next poll will re-enroll")
    else:
        print(f"token for {worker_id} valid for {remaining}s")
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
