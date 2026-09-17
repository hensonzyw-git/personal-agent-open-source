"""Adversarial tests for the deterministic commit executor (R09-A3).

The executor is the only component that turns a capability into a candidate
commit, and only inside a synthetic repo. The frozen contract (拆解 DAL-031,
技术方案 §9.3):

- commit trailers carry exactly `Feature-Id`/`Task-Id`/`Plan-Hash`/
  `Review-Id`, written to disk and read back from the real commit object
  (never from the caller's arguments);
- the binding commits **tree SHAs**: `base_sha` is the base tree the
  candidate must sit on, `result_sha` is the tree the declared change
  deterministically produces (the controller computes it at issue time
  with the same helper the tests use);
- the executor stages exactly the declared paths — unrelated worktree
  noise is never committed;
- precondition drift (HEAD tree no longer the bound base) and git
  failures surface as typed refusals with no commit formed;
- a tampered environment (GIT_DIR, GIT_INDEX_FILE, GIT_AUTHOR_*) and a
  hostile pre-commit hook cannot redirect or influence the commit — the
  executor scrubs the subprocess env and neutralizes hooks;
- through the composed controller: a revoked or expired capability
  refuses before any git call with zero writes, an out-of-set touched
  path lands the block evidence with the stray commit left as evidence
  and the capability unconsumed, and a response-loss replay of the whole
  composition returns the original receipt without a second commit.

The git operations run against a temporary synthetic repo (Roadmap
R09-A2/A3 boundary: never a Personal Agent worktree).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from personal_agent_core.ids import new_id
from personal_agent_dal.machine import commit_controller, commit_executor
from personal_agent_dal.machine.commit_controller import execute_candidate_commit
from personal_agent_dal.machine import commit_capability
from personal_agent_dal.machine.commit_executor import (
    run_candidate_commit,
    result_tree_for,
)
from personal_agent_dal.machine.commit_capability_store import (
    consume_commit_capability_row,
    issue_commit_capability_row,
)
from personal_agent_dal.errors import DalError, DalErrorCode
from personal_agent_dal.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent_dal.storage.machine_models import (
    CommitCapability,
    ExternalEffect,
    TransitionReceipt,
)
from personal_agent_dal.storage.models import Feature
from tests.dal.factories import feature_row
from tests.dal.test_commit_capability import (
    EPOCH,
    LATER,
    NOW,
    TRAILERS,
    _binding,
    _issue_facts,
)

REPO_ID = "dal-pilot-sandbox"
IDENTITY = "git-executor:op-0001"
DECLARED = {"src/app/core.py": "value = 2\n"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _head_commit(repo_path: Path) -> str:
    return _git(repo_path, "rev-parse", "HEAD").stdout.strip()


def _head_tree(repo_path: Path) -> str:
    return _git(repo_path, "rev-parse", "HEAD^{tree}").stdout.strip()


def _commit_count(repo_path: Path) -> int:
    return int(_git(repo_path, "rev-list", "--count", "HEAD").stdout.strip())


@pytest.fixture()
def engine(tmp_path):
    engine = create_database_engine(tmp_path / "dal.sqlite")
    create_all(engine)
    # The block landing moves a real feature row through the engine; the
    # composition requires the platform's verified feature to exist.
    sessions = session_factory(engine)
    with sessions() as session, session.begin():
        session.add(
            feature_row(
                feature_id="feature-0001",
                version=5,
                state="verified",
            )
        )
    return engine


@pytest.fixture()
def repo(tmp_path) -> tuple[Path, str]:
    """A synthetic repo with one commit; returns (path, base commit sha).

    The layout matches the frozen test allowlist (``src/app`` directory,
    ``tests/app/test_service.py`` file) so the clean path exercises
    in-set touches; the out-of-set test adds ``deploy/notes.txt``.
    """
    repo_path = tmp_path / "synthetic-repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-q", "-b", "main", str(repo_path))
    _git(repo_path, "config", "user.email", "dal@example.com")
    _git(repo_path, "config", "user.name", "DAL Executor Test")
    (repo_path / "src" / "app").mkdir(parents=True)
    (repo_path / "src" / "app" / "core.py").write_text("value = 1\n")
    (repo_path / "tests" / "app").mkdir(parents=True)
    (repo_path / "tests" / "app" / "test_service.py").write_text(
        "def test_ok(): pass\n"
    )
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-qm", "init")
    return repo_path, _head_commit(repo_path)


def _binding_for(repo_path: Path, files: dict[str, str] | None = None) -> dict:
    """A binding whose base/result SHAs are the *real* trees of this repo
    under the declared change — what the controller computes at issue time."""
    binding = _binding()
    binding["base_sha"] = _head_tree(repo_path)
    binding["result_sha"] = result_tree_for(
        repo_path, binding["base_sha"], files=files or DECLARED
    )
    binding["capability_id"] = new_id()
    binding["trailers"] = dict(TRAILERS)
    return binding


def _compose(engine, repo_path: Path, binding: dict, *, declared=("src/app/core.py",),
             identity: str = IDENTITY, now: int = NOW,
             repository_id: str = REPO_ID):
    return execute_candidate_commit(
        engine,
        repo_path,
        issue_facts=_issue_facts(binding=binding),
        repository_id=repository_id,
        now=now,
        current_epoch=EPOCH,
        current_lease_epoch=7,
        declared_paths=list(declared),
        message="candidate: update core",
        consumed_by=identity,
    )


def _row(engine) -> CommitCapability:
    sessions = session_factory(engine)
    with sessions() as session:
        return session.scalars(
            select(CommitCapability).where(
                CommitCapability.capability_id == session.scalars(
                    select(CommitCapability.capability_id)
                ).first()
            )
        ).one()


def _intent_keys(engine) -> set[str]:
    sessions = session_factory(engine)
    with sessions() as session:
        return set(session.scalars(select(ExternalEffect.remote_idempotency_key)))


# --- the executor: clean path ---------------------------------------------------


def test_candidate_commit_forms_with_disk_trailers(repo):
    repo_path, base_commit = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])

    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is None
    assert len(result.commit_sha) == 40
    assert result.parent_commit_sha == base_commit
    # The commit's tree is exactly the binding's result tree, and the
    # parent commit's tree is exactly the binding's base tree.
    assert _git(repo_path, "rev-parse", "HEAD^{tree}").stdout.strip() == (
        binding["result_sha"]
    )
    assert _git(repo_path, "rev-parse", f"{base_commit}^{{tree}}").stdout.strip() == (
        binding["base_sha"]
    )
    # The trailers are read back from the real commit object on disk.
    raw = _git(repo_path, "log", "-1", "--format=%B").stdout
    for key in ("Feature-Id", "Task-Id", "Plan-Hash", "Review-Id"):
        assert f"{key}: " in raw
    assert result.trailers_readback == {
        "Feature-Id": TRAILERS["Feature-Id"],
        "Task-Id": TRAILERS["Task-Id"],
        "Plan-Hash": TRAILERS["Plan-Hash"],
        "Review-Id": TRAILERS["Review-Id"],
    }
    assert result.touched_paths == ("src/app/core.py",)
    # The declared path is committed. The executor never writes the shared
    # index, so the index still holds the base state and git shows the
    # committed-elsewhere change as a staged modification of the declared
    # path only — no other entry may appear.
    status = _git(repo_path, "status", "--porcelain").stdout.splitlines()
    assert status == ["MM src/app/core.py"]


def test_executor_stages_exactly_the_declared_paths(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    # Unrelated worktree noise: an untracked file and an undeclared tracked
    # modification. Neither may enter the candidate commit.
    (repo_path / "README-untouched.txt").write_text("untracked noise\n")
    (repo_path / "tests" / "app" / "test_service.py").write_text("def test_drifted(): pass\n")

    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is None
    # The commit's tree matches the binding (declared content only), and the
    # noise stayed in the worktree.
    assert _git(repo_path, "rev-parse", "HEAD^{tree}").stdout.strip() == (
        binding["result_sha"]
    )
    assert result.touched_paths == ("src/app/core.py",)
    status = _git(repo_path, "status", "--porcelain").stdout
    assert "README-untouched.txt" in status
    assert "tests/app/test_service.py" in status


def test_executor_tree_is_deterministic(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    r1 = run_candidate_commit(
        repo_path, binding=binding, declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert r1.refusal is None
    _git(repo_path, "reset", "--hard", "-q", "HEAD~1")
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    r2 = run_candidate_commit(
        repo_path, binding=binding, declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert r2.refusal is None
    # Same declared content on the same base always yields the same tree;
    # commit SHAs differ (timestamps), trees do not.
    assert r1.tree_sha == r2.tree_sha == binding["result_sha"]


# --- the executor: precondition drift and git hygiene ----------------------------


def test_head_tree_drift_refuses_without_commit(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    # Move the branch after issuing: the base tree the capability bound no
    # longer matches the worktree HEAD.
    (repo_path / "src" / "app" / "core.py").write_text("value = 3\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-qm", "intermediate drift")
    count = _commit_count(repo_path)

    result = run_candidate_commit(
        repo_path, binding=binding, declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.commit_sha is None
    assert result.refusal is not None
    assert "base" in result.refusal.reason
    assert _commit_count(repo_path) == count  # no candidate added


def test_noop_declared_path_refuses(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    # Declared content identical to the base: nothing to commit.
    (repo_path / "src" / "app" / "core.py").write_text("value = 1\n")

    result = run_candidate_commit(
        repo_path, binding=binding, declared_paths=["src/app/core.py"],
        message="candidate: noop",
    )
    assert result.commit_sha is None
    assert result.refusal is not None
    assert _commit_count(repo_path) == 1


def test_declared_dotgit_path_is_refused(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)

    result = run_candidate_commit(
        repo_path, binding=binding, declared_paths=[".git/config"],
        message="candidate: .git touch",
    )
    assert result.commit_sha is None
    assert result.refusal is not None
    assert ".git" in result.refusal.reason
    assert _commit_count(repo_path) == 1


def test_empty_declared_paths_refuse(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)

    result = run_candidate_commit(
        repo_path, binding=binding, declared_paths=[], message="candidate: empty",
    )
    assert result.commit_sha is None
    assert result.refusal is not None


def test_orphan_head_is_surfaced_as_refusal(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    # Unborn HEAD: rev-parse fails; the executor must surface a typed
    # refusal, never a crash and never a silent pass.
    _git(repo_path, "checkout", "--orphan", "detached-test")

    result = run_candidate_commit(
        repo_path, binding=binding, declared_paths=["src/app/core.py"],
        message="candidate: orphan",
    )
    assert result.commit_sha is None
    assert result.refusal is not None


# --- the executor: hostile environment and hooks ---------------------------------


def test_hostile_environment_and_hooks_cannot_influence_the_commit(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    # A hostile pre-commit hook: if it runs, it leaves a marker and fails
    # the commit.
    hooks = repo_path / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(
        "#!/bin/sh\ntouch HOOK_RAN\nexit 1\n"
    )
    hook.chmod(0o755)

    hostile = {
        "GIT_DIR": "/nonexistent/hostile.git",
        "GIT_WORK_TREE": "/nonexistent",
        "GIT_INDEX_FILE": "/nonexistent/hostile-index",
        "GIT_AUTHOR_NAME": "Hostile Author",
        "GIT_COMMITTER_NAME": "Hostile Committer",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/nonexistent/hostile-hooks",
    }
    saved = {key: os.environ.get(key) for key in hostile}
    os.environ.update(hostile)
    try:
        result = run_candidate_commit(
            repo_path, binding=binding, declared_paths=["src/app/core.py"],
            message="candidate: update core",
        )
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert result.refusal is None
    assert _git(repo_path, "rev-parse", "HEAD^{tree}").stdout.strip() == (
        binding["result_sha"]
    )
    assert not (repo_path / "HOOK_RAN").exists()
    # The author comes from the repo's local config, not the hostile env.
    author = _git(repo_path, "log", "-1", "--format=%an").stdout.strip()
    assert author == "DAL Executor Test"


def test_executor_ignores_path_filters_and_post_index_hooks(repo, tmp_path, monkeypatch):
    """All three used to execute before or outside ``git commit`` itself."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])

    marker = tmp_path / "unexpected-execution"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(f"#!/bin/sh\ntouch {marker}\nexit 99\n")
    fake_git.chmod(0o755)
    filter_script = tmp_path / "filter.sh"
    filter_script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    filter_script.chmod(0o755)
    (repo_path / ".gitattributes").write_text("src/app/core.py filter=evil\n")
    _git(repo_path, "config", "filter.evil.clean", str(filter_script))
    hook = repo_path / ".git" / "hooks" / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")

    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )

    assert result.refusal is None
    assert not marker.exists()


def test_executor_requires_an_exact_declared_change_set(repo):
    repo_path, _ = repo
    files = {
        "src/app/core.py": DECLARED["src/app/core.py"],
        "tests/app/test_service.py": "def test_ok(): pass\n",
    }
    binding = _binding_for(repo_path, files)
    (repo_path / "src" / "app" / "core.py").write_text(files["src/app/core.py"])
    before = _commit_count(repo_path)

    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=list(files),
        message="candidate: exact-set",
    )

    assert result.refusal is not None
    assert "diverged" in result.refusal.reason
    assert _commit_count(repo_path) == before


def test_post_commit_validation_failure_cannot_move_head(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    before = _head_commit(repo_path)

    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate\n\nFeature-Id: injected",
    )

    assert result.refusal is not None
    assert _head_commit(repo_path) == before
    assert _commit_count(repo_path) == 1


# --- the composed controller ------------------------------------------------------


def test_composition_consumes_and_records_one_commit(engine, repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])

    outcome = _compose(engine, repo_path, binding)
    assert outcome.phase == "consumed"
    assert outcome.replayed is False
    assert len(outcome.commit_sha) == 40
    assert outcome.violations == ()

    row = _row(engine)
    assert row.state == "consumed"
    assert row.state_version == 2
    assert row.consumed_by == IDENTITY
    assert row.result_sha == binding["result_sha"]
    assert _commit_count(repo_path) == 2
    assert f"{IDENTITY}:intent" in _intent_keys(engine)


def test_composition_replay_returns_receipt_without_second_commit(engine, repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])

    first = _compose(engine, repo_path, binding)
    assert first.phase == "consumed"
    second = _compose(engine, repo_path, binding)
    assert second.phase == "replayed"
    assert second.replayed is True
    # The original receipt, and no second commit formed by the replay.
    assert _commit_count(repo_path) == 2
    row = _row(engine)
    assert row.consumed_by == IDENTITY
    assert row.uses_consumed == 1


def test_blocked_composition_replay_returns_original_block_receipt(engine, repo):
    """A block does not consume the capability, so a response-loss replay of
    the whole composition must not re-enter the executor: the answer is the
    original block receipt, and no second stray commit forms — even when the
    worktree has been reset so git would happily form one."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "deploy").mkdir()
    (repo_path / "deploy" / "notes.txt").write_text("escape attempt\n")

    first = _compose(engine, repo_path, binding, declared=["deploy/notes.txt"])
    assert first.phase == "blocked"
    assert _commit_count(repo_path) == 2

    # The hostile replay shape: the stray commit is wiped from the branch so
    # the executor could form a second one if the fence failed. (The stray
    # commit object stays in the store's evidence history; only the ref
    # moves back.)
    _git(repo_path, "reset", "--hard", "-q", "HEAD~1")

    second = _compose(engine, repo_path, binding, declared=["deploy/notes.txt"])
    assert second.phase == "blocked"
    assert second.replayed is True
    # No second stray commit, no duplicate block intent, no crash.
    assert _commit_count(repo_path) == 1
    row = _row(engine)
    assert row.state == "issued"  # still not consumed
    block_intents = [
        key for key in _intent_keys(engine) if key == f"{IDENTITY}:block"
    ]
    assert len(block_intents) == 1
    # The engine's block landing is also not duplicated: the receipt key is
    # present exactly once (the unique constraint held).
    sessions = session_factory(engine)
    with sessions() as session:
        feature = session.scalars(
            select(Feature).where(Feature.feature_id == "feature-0001")
        ).one()
    assert feature.state == "needs_human"


def test_pending_block_recovers_engine_transition_without_rerunning_git(engine, repo):
    """A crash after the store commit leaves a pending record, not success."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    issue_facts = _issue_facts(binding=binding)
    issue_commit_capability_row(engine, issue_facts, repository_id=REPO_ID)
    pending_facts = {
        "schema_version": commit_capability.CONSUME_FACTS_SCHEMA,
        "target": issue_facts["target"],
        "capability": {
            **binding,
            "uses_consumed": 0,
            "consumed_by": None,
            "revoked_at": None,
        },
        "presented": {
            "capability_id": binding["capability_id"],
            "approval_id": binding["approval_id"],
            "base_sha": binding["base_sha"],
            "result_sha": binding["result_sha"],
            "touched_paths": ["deploy/notes.txt"],
            "trailers": dict(binding["trailers"]),
            "idempotency_key": binding["idempotency_key"],
        },
        "now": NOW,
        "current_epoch": EPOCH,
        "current_lease_epoch": 7,
    }
    pending = consume_commit_capability_row(
        engine, pending_facts, consumed_by=IDENTITY
    )
    assert pending.verdict == "blocked"
    assert _commit_count(repo_path) == 1

    replay = _compose(engine, repo_path, binding)
    assert replay.phase == "blocked"
    assert replay.replayed is True
    assert _commit_count(repo_path) == 1
    sessions = session_factory(engine)
    with sessions() as session:
        feature = session.scalars(
            select(Feature).where(Feature.feature_id == "feature-0001")
        ).one()
    assert feature.state == "needs_human"


def test_pending_block_recovers_after_consumer_identity_changes(engine, repo):
    """A restart may mint a new command identity after response loss."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    issue_facts = _issue_facts(binding=binding)
    issue_commit_capability_row(engine, issue_facts, repository_id=REPO_ID)
    pending_facts = {
        "schema_version": commit_capability.CONSUME_FACTS_SCHEMA,
        "target": issue_facts["target"],
        "capability": {
            **binding,
            "uses_consumed": 0,
            "consumed_by": None,
            "revoked_at": None,
        },
        "presented": {
            "capability_id": binding["capability_id"],
            "approval_id": binding["approval_id"],
            "base_sha": binding["base_sha"],
            "result_sha": binding["result_sha"],
            "touched_paths": ["deploy/notes.txt"],
            "trailers": dict(binding["trailers"]),
            "idempotency_key": binding["idempotency_key"],
        },
        "now": NOW,
        "current_epoch": EPOCH,
        "current_lease_epoch": 7,
    }
    assert (
        consume_commit_capability_row(
            engine, pending_facts, consumed_by=IDENTITY
        ).verdict
        == "blocked"
    )

    replay = _compose(
        engine,
        repo_path,
        binding,
        identity="git-executor:op-after-restart",
    )
    assert replay.phase == "blocked"
    assert replay.replayed is True
    assert _commit_count(repo_path) == 1


def test_retry_recovers_a_commit_formed_before_consume(engine, repo, monkeypatch):
    """A post-git/pre-store crash must consume the read-back HEAD, not rerun git."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    original = commit_controller.consume_commit_capability_row

    def crash_before_store(*args, **kwargs):
        raise RuntimeError("synthetic crash before consume")

    monkeypatch.setattr(
        commit_controller,
        "consume_commit_capability_row",
        crash_before_store,
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        _compose(engine, repo_path, binding)
    assert _commit_count(repo_path) == 2

    monkeypatch.setattr(
        commit_controller,
        "consume_commit_capability_row",
        original,
    )
    retry = _compose(engine, repo_path, binding)
    assert retry.phase == "consumed"
    assert retry.commit_sha == _head_commit(repo_path)
    assert _commit_count(repo_path) == 2
    assert _row(engine).state == "consumed"


def test_index_sync_failure_refuses_before_head_moves(repo, monkeypatch):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    before = _head_commit(repo_path)
    original = commit_executor._git

    def fail_candidate_index(repo_arg, *args, env=None, input_text=None):
        if args == ("read-tree", binding["result_sha"]) and env is not None and (
            "GIT_INDEX_FILE" in env
        ):
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        return original(repo_arg, *args, env=env, input_text=input_text)

    monkeypatch.setattr(commit_executor, "_git", fail_candidate_index)
    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is not None
    assert "index" in result.refusal.reason
    assert _head_commit(repo_path) == before


def test_symlink_swap_cannot_read_outside_the_repository(repo, tmp_path, monkeypatch):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    candidate = repo_path / "src" / "app" / "core.py"
    candidate.write_text(DECLARED["src/app/core.py"])
    outside = tmp_path / "outside-secret"
    outside.write_text("must not enter a git object\n")
    original_open = commit_executor.os.open
    swapped = False

    def race_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == "core.py" and kwargs.get("dir_fd") is not None and not swapped:
            candidate.unlink()
            candidate.symlink_to(outside)
            swapped = True
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(commit_executor.os, "open", race_open)
    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is not None
    assert _commit_count(repo_path) == 1


def test_executor_refuses_an_executable_declared_file(repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    candidate = repo_path / "src" / "app" / "core.py"
    candidate.write_text(DECLARED["src/app/core.py"])
    candidate.chmod(0o755)
    before = _head_commit(repo_path)

    result = run_candidate_commit(
        repo_path,
        binding=binding,
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is not None
    assert "non-executable" in result.refusal.reason
    assert _head_commit(repo_path) == before


# --- third-review remediation: torn actuals and shared-index isolation ----------


def test_head_actuals_derive_everything_from_one_parsed_commit(repo, monkeypatch):
    """B-1: the four former rev-parse rounds could observe different HEADs.

    Every actual must derive from the single ``head_commit`` SHA parsed once:
    tree via ``<sha>^{tree}``, parent via ``<sha>^``, paths and message via
    that immutable object. A ref flip between git calls then cannot compose
    actuals for a commit that never existed.
    """
    repo_path, _ = repo
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    binding = _binding_for(repo_path)
    result_tree = binding["result_sha"]

    fixed = {"GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
             "PATH": "/usr/bin:/bin"}
    # Commit A: correct trailers, wrong tree. Commit B: correct tree, wrong
    # trailers. Both sit on the base, so they share the binding's base tree.
    (repo_path / "src" / "app" / "core.py").write_text("value = A_wrong\n")
    _git(repo_path, "add", ".")
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-qm",
         "wrong tree, right trailers\n\n"
         + "\n".join(f"{k}: {v}" for k, v in TRAILERS.items())],
        env=fixed, check=True, capture_output=True, text=True,
    )
    sha_a = _head_commit(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    _git(repo_path, "add", ".")
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-qm",
         "right tree, wrong trailers\n\nWrong-Id: x"],
        env=fixed, check=True, capture_output=True, text=True,
    )
    sha_b = _head_commit(repo_path)
    _git(repo_path, "update-ref", "HEAD", sha_a)  # first parse must see A

    original_rev = commit_executor._rev
    flipped = {"v": False}

    def flip_after_first_parse(repo_arg, spec):
        if spec == "HEAD" and not flipped["v"]:
            flipped["v"] = True
            value = original_rev(repo_arg, spec)  # parses on A
            _git(repo_arg, "update-ref", "HEAD", sha_b)  # later parses see B
            return value
        return original_rev(repo_arg, spec)

    monkeypatch.setattr(commit_executor, "_rev", flip_after_first_parse)
    result = commit_executor.read_head_commit_actuals(repo_path)

    # Whatever the flip did, the returned actuals must all belong to ONE real
    # commit object: either A's or B's, never a mix.
    mixed = (
        result.refusal is None
        and (
            (result.commit_sha == sha_a)
            != (result.tree_sha == _git(repo_path, "rev-parse", f"{sha_a}^{{tree}}").stdout.strip())
            or (result.commit_sha == sha_b)
            != (result.tree_sha == _git(repo_path, "rev-parse", f"{sha_b}^{{tree}}").stdout.strip())
        )
    )
    assert not mixed, (
        "actuals tore across commits: "
        f"sha={result.commit_sha} tree={result.tree_sha}"
    )
    # And the correct-tree commit B stays recoverable: after the flip its
    # actuals are exactly B's object read-back.
    if result.commit_sha == sha_b:
        assert result.tree_sha == result_tree
        assert result.trailers_readback == {"Wrong-Id": "x"}


def test_head_actuals_refuse_when_head_vanishes_mid_read(repo, monkeypatch):
    """A ref moved away mid-read must refuse, not compose partial actuals."""
    repo_path, _ = repo
    original_rev = commit_executor._rev
    calls = {"n": 0}

    def delete_head_after_first(repo_arg, spec):
        calls["n"] += 1
        value = original_rev(repo_arg, spec)
        if calls["n"] == 1:
            _git(repo_arg, "update-ref", "-d", "HEAD")
        return value

    monkeypatch.setattr(commit_executor, "_rev", delete_head_after_first)
    result = commit_executor.read_head_commit_actuals(repo_path)
    assert result.refusal is not None
    assert result.commit_sha is None


def test_executor_never_mutates_the_shared_index(repo, monkeypatch):
    """B-2: the shared index is not the executor's to write.

    The frozen threat model gives a branch one writer (lease + CAS + branch
    lock), but a crashed/failed call must leave the index exactly as the
    current HEAD defines it: no pre-ref sync into the shared index, no
    restore on CAS failure. Every ``read-tree`` the executor runs must
    target a private ``GIT_INDEX_FILE``.
    """
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    original_git = commit_executor._git

    def reject_shared_index_writes(repo_arg, *args, env=None, input_text=None):
        if args and args[0] == "read-tree" and env is not None and (
            "GIT_INDEX_FILE" not in env
        ):
            raise AssertionError("executor wrote the shared index")
        return original_git(repo_arg, *args, env=env, input_text=input_text)

    monkeypatch.setattr(commit_executor, "_git", reject_shared_index_writes)
    result = run_candidate_commit(
        repo_path,
        binding={"base_sha": binding["base_sha"],
                 "trailers": binding["trailers"]},
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is None
    assert _commit_count(repo_path) == 2


def test_cas_failure_leaves_the_shared_index_untouched(repo, monkeypatch):
    """A concurrent writer's staging survives the executor's failed CAS."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    # A concurrent writer's file exists unstaged; the writer stages and
    # commits it while the executor is between its staged-set precheck and
    # the CAS (hooked below), so the pre-existing-staged precheck passes.
    (repo_path / "concurrent.txt").write_text("written by someone else\n")
    original_git = commit_executor._git
    writer_moved = {"v": False}

    def move_head_at_commit_time(repo_arg, *args, env=None, input_text=None):
        if args[:1] == ("commit-tree",) and not writer_moved["v"]:
            # The concurrent writer stages and commits while the executor is
            # between its staged-set precheck and the CAS: their staged
            # entry lands in their commit, HEAD moves, and the executor's
            # CAS then fails.
            writer_moved["v"] = True
            subprocess.run(
                ["git", "-C", str(repo_arg), "add", "concurrent.txt"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_arg), "commit", "-qm", "concurrent"],
                env={"GIT_AUTHOR_NAME": "W", "GIT_AUTHOR_EMAIL": "w@x",
                     "GIT_COMMITTER_NAME": "W", "GIT_COMMITTER_EMAIL": "w@x",
                     "PATH": "/usr/bin:/bin"},
                check=True, capture_output=True, text=True,
            )
        return original_git(repo_arg, *args, env=env, input_text=input_text)

    monkeypatch.setattr(commit_executor, "_git", move_head_at_commit_time)
    result = run_candidate_commit(
        repo_path,
        binding={"base_sha": binding["base_sha"],
                 "trailers": binding["trailers"]},
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is not None
    assert "head moved" in result.refusal.reason
    # The writer's commit survived with their own content and the executor
    # left nothing in the shared index: no candidate content, no restore.
    assert _git(repo_path, "show", "HEAD:src/app/core.py").stdout == "value = 1\n"
    assert _git(repo_path, "show", "HEAD:concurrent.txt").stdout == (
        "written by someone else\n"
    )
    assert _git(repo_path, "diff", "--cached", "--name-only").stdout.strip() == ""


def test_git_timeout_becomes_a_typed_refusal(repo, monkeypatch):
    """S-1: an expired git subprocess refuses instead of raising."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    original_run = commit_executor.subprocess.run

    def expire(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=60)

    monkeypatch.setattr(commit_executor.subprocess, "run", expire)
    result = run_candidate_commit(
        repo_path,
        binding={"base_sha": binding["base_sha"],
                 "trailers": binding["trailers"]},
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is not None
    assert result.refusal.reason == "git command did not complete in time"
    assert result.commit_sha is None


def test_orphan_head_recovery_carries_a_typed_refusal(repo):
    """S-3: an orphan-root drift is an operator case, not base-tree drift."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    # Rewrite history to a parentless root whose tree differs from the base.
    (repo_path / "src" / "app" / "core.py").write_text("value = 777\n")
    _git(repo_path, "checkout", "-q", "--orphan", "rewritten")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-qm", "hostile: orphan root")
    _git(repo_path, "branch", "-q", "-M", "rewritten", "main")

    result = run_candidate_commit(
        repo_path,
        binding={"base_sha": binding["base_sha"],
                 "trailers": binding["trailers"]},
        declared_paths=["src/app/core.py"],
        message="candidate: update core",
    )
    assert result.refusal is not None
    assert result.refusal.reason == (
        "base tree drift: head tree no longer matches the issued base"
    )
    # The recovery reader must then refuse the parentless head with its own
    # fixed phrase, distinguishable from a plain base-tree drift.
    recovered = commit_executor.read_head_commit_actuals(repo_path)
    assert recovered.refusal is not None
    assert "parent" in recovered.refusal.reason


def test_controller_surfaces_orphan_drift_without_block_or_consume(engine, repo):
    """The composition answers an orphan drift as issued + operator phrase."""
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text("value = 777\n")
    _git(repo_path, "checkout", "-q", "--orphan", "rewritten")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-qm", "hostile: orphan root")
    _git(repo_path, "branch", "-q", "-M", "rewritten", "main")

    outcome = _compose(engine, repo_path, binding)
    assert outcome.phase == "issued"
    # The base-tree drift is the first refusal; the recovery read adds the
    # orphan's own typed phrase so the operator sees why reconcile failed.
    assert any(
        "base tree drift" in v for v in outcome.violations
    )
    assert any("no readable parent" in v for v in outcome.violations)
    with session_factory(engine)() as session:
        feature = session.get(Feature, "feature-0001")
    assert feature.state == "verified"


def test_recovery_presentation_of_a_head_with_missing_trailers_blocks(engine, repo):
    """S-2: recovery actuals never borrow the binding's trailers.

    A drifted head whose message lacks the frozen trailers must reach the
    consume gate with its real (empty) trailer set and be policy-blocked,
    not silently repaired into the binding's trailer values.
    """
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    _git(repo_path, "add", "src/app/core.py")
    fixed = {"GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
             "PATH": "/usr/bin:/bin"}
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-qm",
         "right tree, no trailers"],
        env=fixed, check=True, capture_output=True, text=True,
    )
    outcome = _compose(engine, repo_path, binding)
    assert outcome.phase == "blocked"
    assert any("trailer" in v for v in outcome.violations)
    with session_factory(engine)() as session:
        feature = session.get(Feature, "feature-0001")
    assert feature.state == "needs_human"
    assert _commit_count(repo_path) == 2  # stray head kept as evidence


def test_completed_block_replay_rejects_a_drifted_repository_id(engine, repo):
    """S-2: the completed-block early return re-enters the issue fence.

    The setup must land a REAL engine block receipt (a store-only pending
    block never reaches ``_has_block_receipt``, and the drift would then be
    rejected by the ordinary issue path with the fence's presence
    irrelevant — the vacuous shape the fourth review killed). The full
    composition with an out-of-set touch is what lands the receipt, so the
    replay below actually enters the early return whose fence is under
    test.
    """
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "deploy").mkdir()
    (repo_path / "deploy" / "notes.txt").write_text("escape attempt\n")
    first = _compose(engine, repo_path, binding, declared=["deploy/notes.txt"])
    assert first.phase == "blocked"
    sessions = session_factory(engine)
    with sessions() as session:
        assert (
            session.scalars(
                select(TransitionReceipt).where(
                    TransitionReceipt.idempotency_key
                    == f"{binding['idempotency_key']}:block"
                )
            ).first()
            is not None
        ), "precondition: the engine block receipt must exist"

    # Replay with a drifted repository_id: the completed-block early return
    # re-enters the issue fence, which must conflict on the changed
    # composition-level repository identity instead of answering the
    # completed block for altered authority content.
    with pytest.raises(DalError) as excinfo:
        _compose(engine, repo_path, binding, repository_id="dal-other-repo")
    assert excinfo.value.code == DalErrorCode.IDEMPOTENCY_CONFLICT


def test_recovery_of_a_matching_tree_on_a_different_parent_consumes(engine, repo):
    """S-2: the binding binds TREE SHAs only, so a head whose tree and parent
    tree both match the binding is recoverable into a consume regardless of
    which commit object carries that tree — commit identity is not part of
    the contract. The genuinely drifting property (parent TREE differs from
    the bound base) blocks, and is pinned by the next test.

    Built via plumbing: amend the base commit (same tree, different
    identity), then hang the binding's result tree on it as a parent.
    """
    repo_path, base_commit = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    # Build a second parent with the same tree as base but a different
    # identity (amended timestamp), then hang the candidate tree on it.
    env = {"GIT_AUTHOR_DATE": "@1 +0000", "GIT_COMMITTER_DATE": "@1 +0000",
           "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "X",
           "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "X",
           "GIT_COMMITTER_EMAIL": "x@x"}
    subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-q", "--amend", "-m",
         "same tree, different parent commit", "--date=@1"],
        env=env, check=True, capture_output=True, text=True,
    )
    amended = _head_commit(repo_path)
    assert _git(repo_path, "rev-parse", f"{amended}^{{tree}}").stdout.strip() == (
        binding["base_sha"]
    )
    assert amended != base_commit
    # Candidate = result tree on top of the amended parent.
    message = "candidate: update core\n\n" + "\n".join(
        f"{k}: {v}" for k, v in TRAILERS.items()
    )
    forged = subprocess.run(
        ["git", "-C", str(repo_path), "commit-tree", binding["result_sha"],
         "-p", amended, "-m", message],
        env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(repo_path, "update-ref", "HEAD", forged)
    outcome = _compose(engine, repo_path, binding)
    # The binding binds tree SHAs only: a head whose tree AND parent tree
    # both match the binding is recoverable regardless of commit identity.
    assert outcome.phase == "consumed"
    assert outcome.commit_sha == forged
    assert _row(engine).state == "consumed"


def test_recovery_of_a_drifted_parent_tree_blocks(engine, repo):
    """The real parent-drift property: parent tree != bound base blocks."""
    repo_path, base_commit = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])
    # Intermediate commit genuinely changes the tree (drifted parent).
    (repo_path / "tests" / "app" / "test_service.py").write_text(
        "def test_intermediate(): pass\n"
    )
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-qm", "intermediate")
    intermediate = _head_commit(repo_path)
    # Forge the result tree on top of the intermediate parent.
    env = {"GIT_AUTHOR_DATE": "@1 +0000", "GIT_COMMITTER_DATE": "@1 +0000",
           "PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "X",
           "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "X",
           "GIT_COMMITTER_EMAIL": "x@x"}
    message = "candidate: update core\n\n" + "\n".join(
        f"{k}: {v}" for k, v in TRAILERS.items()
    )
    forged = subprocess.run(
        ["git", "-C", str(repo_path), "commit-tree", binding["result_sha"],
         "-p", intermediate, "-m", message],
        env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    _git(repo_path, "update-ref", "HEAD", forged)
    outcome = _compose(engine, repo_path, binding)
    assert outcome.phase == "blocked"
    assert any("base_sha" in v for v in outcome.violations)


def test_controller_rejects_invalid_time_before_issue_or_git(engine, repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    (repo_path / "src" / "app" / "core.py").write_text(DECLARED["src/app/core.py"])

    with pytest.raises(DalError) as excinfo:
        _compose(engine, repo_path, binding, now=-1)
    assert excinfo.value.code == DalErrorCode.INVALID_ARGUMENT
    assert _commit_count(repo_path) == 1
    assert _intent_keys(engine) == set()


def test_controller_rejects_missing_feature_before_issue_or_git(tmp_path, repo):
    repo_path, _ = repo
    engine = create_database_engine(tmp_path / "empty.sqlite")
    create_all(engine)
    binding = _binding_for(repo_path)
    (repo_path / "deploy").mkdir()
    (repo_path / "deploy" / "notes.txt").write_text("escape attempt\n")

    with pytest.raises(DalError) as excinfo:
        _compose(
            engine, repo_path, binding, declared=["deploy/notes.txt"]
        )
    assert excinfo.value.code == DalErrorCode.INVALID_ARGUMENT
    assert _commit_count(repo_path) == 1
    assert _intent_keys(engine) == set()


def test_composition_refuses_revoked_capability_without_git(engine, repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    issue_commit_capability_row(
        engine, _issue_facts(binding=binding), repository_id=REPO_ID
    )
    # Revoke between issue and execution.
    sessions = session_factory(engine)
    with sessions() as session:
        row = session.scalars(
            select(CommitCapability).where(
                CommitCapability.capability_id == binding["capability_id"]
            )
        ).one()
        row.revoked_at = 1_800_000_100
        session.commit()

    outcome = _compose(engine, repo_path, binding)
    assert outcome.phase == "stale"
    assert outcome.commit_sha is None
    # Zero git, and the only effect rows are the issue's own.
    assert _commit_count(repo_path) == 1
    assert _intent_keys(engine) == {f"issue-key-0001:intent"}


def test_composition_refuses_expired_capability_without_git(engine, repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    issue_commit_capability_row(
        engine, _issue_facts(binding=binding), repository_id=REPO_ID
    )

    outcome = _compose(engine, repo_path, binding, now=LATER + 10)
    assert outcome.phase == "stale"
    assert outcome.commit_sha is None
    assert _commit_count(repo_path) == 1


def test_out_of_set_touch_lands_block_with_stray_commit(engine, repo):
    repo_path, _ = repo
    binding = _binding_for(repo_path)
    # The declared change escapes the issued allowed set: the executor
    # forms the commit (the gate judges actuals), the consume gate lands
    # the block evidence, and the capability is NOT consumed.
    (repo_path / "deploy").mkdir()
    (repo_path / "deploy" / "notes.txt").write_text("escape attempt\n")

    outcome = _compose(engine, repo_path, binding, declared=["deploy/notes.txt"])
    assert outcome.phase == "blocked"
    assert any("outside the allowed set" in v for v in outcome.violations)
    assert outcome.commit_sha is not None  # the stray commit is the evidence
    row = _row(engine)
    assert row.state == "issued"  # not consumed
    assert _commit_count(repo_path) == 2
    assert f"{IDENTITY}:block" in _intent_keys(engine)
    assert f"{IDENTITY}:intent" not in _intent_keys(engine)
    # The engine's block transition really landed through the frozen
    # registry: the feature is needs_human with the policy reason, and
    # the block receipt exists under the derived command key.
    sessions = session_factory(engine)
    with sessions() as session:
        feature = session.scalars(
            select(Feature).where(Feature.feature_id == "feature-0001")
        ).one()
        receipt_keys = set(
            session.scalars(select(TransitionReceipt.idempotency_key))
        )
    assert feature.state == "needs_human"
    assert feature.reason_code == "POLICY_FAILURE"
    assert feature.reason_owner == "feature"
    assert "issue-key-0001:block" in receipt_keys
