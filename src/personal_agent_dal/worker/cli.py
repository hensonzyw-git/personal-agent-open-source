"""Command-line entry point for the Home Mac Worker.

Subcommands are deliberately one-shot: `poll-once` runs a single
claim/execute/checkpoint/receipt cycle and exits; `healthcheck` reports whether
the config, database and repo allowlist are reachable. launchd calls `poll-once`
on a `StartInterval`; there is no long-lived daemon loop and no listening port.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import text

from personal_agent_dal.storage.engine import create_database_engine
from personal_agent_dal.worker.config import WorkerConfig, load_worker_config
from personal_agent_dal.worker.poll_once import run_poll_once

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="personal-agent-dal-worker")
    parser.add_argument(
        "--config", type=Path, required=True, help="path to the worker config JSON"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("poll-once", help="claim one job, run it, record the result, exit")
    sub.add_parser("healthcheck", help="verify config, database and repos are reachable")
    args = parser.parse_args(argv)

    try:
        config = load_worker_config(args.config)
    except (OSError, ValueError) as error:
        print(f"config error: {type(error).__name__}", file=sys.stderr)
        return 1

    if args.command == "healthcheck":
        return _healthcheck(config)
    return _poll_once(config)


def _poll_once(config: WorkerConfig) -> int:
    if not config.database_path.exists():
        print("database missing: run the DAL migration first", file=sys.stderr)
        return 1
    try:
        engine = create_database_engine(config.database_path)
        try:
            outcome = run_poll_once(engine, config)
        finally:
            engine.dispose()
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
    if not config.database_path.exists():
        problems.append("database_path missing")
    else:
        try:
            engine = create_database_engine(config.database_path)
            try:
                with engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                if revision != "0005":
                    problems.append("database schema is not at worker revision 0005")
            finally:
                engine.dispose()
        except Exception:  # noqa: BLE001 - healthcheck reports a bounded category
            problems.append("database schema unreadable")
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


if __name__ == "__main__":
    raise SystemExit(main())
