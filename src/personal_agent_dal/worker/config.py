"""Worker configuration (DAL-017/018 runnable layer, DAL-R06 transport union).

The repo allowlist, worktree root and run policy live in a small JSON file the
launchd plist points at via `--config`. It is Henson-authored and machine-local;
the loader still validates a closed shape and fails closed on any unknown field,
so a typo cannot silently widen the allowlist.

Schema 1.1 replaces the mandatory top-level `database_path` with a closed
`transport` union. That is the whole point of DAL-R06: a production worker on
the Mac mini must not be able to open the Workflow database at all, and the way
to guarantee that is to make "which database" an option that remote mode simply
does not have. There is deliberately no 1.0 compatibility path — an implicit
"no transport declared, so open the local database" fallback is exactly the
behaviour this schema exists to remove.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Final

from personal_agent_dal.worker.remote import EndpointError, validate_endpoint

CONFIG_SCHEMA: Final[str] = "dal.worker-config/1.1"

#: Known top-level keys; anything else is an error, not ignored.
_KNOWN_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "worker_id",
        "transport",
        "worktree_root",
        "checkpoint_root",
        "kill_switch_path",
        "lease_ttl_seconds",
        "max_attempts",
        "repos",
        "coder_token_path",
    }
)
_REPO_KEYS: Final[frozenset[str]] = frozenset({"local_path"})

_LOCAL_KEYS: Final[frozenset[str]] = frozenset({"mode", "database_path"})
_REMOTE_REQUIRED: Final[frozenset[str]] = frozenset(
    {
        "mode",
        "endpoint",
        "machine_id",
        "capabilities",
        "enrollment_secret_path",
        "token_cache_path",
    }
)
_REMOTE_OPTIONAL: Final[frozenset[str]] = frozenset(
    {
        "ca_bundle_path",
        "request_timeout_seconds",
        "retry_attempts",
        "backoff_base_seconds",
        "backoff_max_seconds",
    }
)

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_RETRY_ATTEMPTS: Final[int] = 4
_DEFAULT_BACKOFF_BASE_SECONDS: Final[float] = 1.0
_DEFAULT_BACKOFF_MAX_SECONDS: Final[float] = 30.0

#: Upper bounds so a config typo cannot turn a one-shot poll into a multi-hour
#: hang (launchd does not start a second poll of the same label while one runs).
_MAX_TIMEOUT_SECONDS: Final[float] = 300.0
_MAX_RETRY_ATTEMPTS: Final[int] = 10
_MAX_BACKOFF_BASE_SECONDS: Final[float] = 60.0
_MAX_BACKOFF_MAX_SECONDS: Final[float] = 300.0


@dataclass(frozen=True)
class RepoAllowlistEntry:
    """One allowlisted repository the worker may touch."""

    repository_id: str
    local_path: str


@dataclass(frozen=True)
class LocalTransportConfig:
    """Queue access through the Workflow SQLite database on this machine."""

    database_path: Path

    mode: ClassVar[str] = "local"

    def protected_paths(self) -> tuple[Path, ...]:
        """Paths no sandboxed command may read or write."""
        return (self.database_path,)


@dataclass(frozen=True)
class RemoteTransportConfig:
    """Queue access through the pinned Dev Workflow Service over HTTPS.

    There is no `database_path` here and that absence is load-bearing: a worker
    configured this way has no way to name a database, so it cannot open one.
    """

    endpoint: str
    machine_id: str
    capabilities: tuple[str, ...]
    enrollment_secret_path: Path
    token_cache_path: Path
    ca_bundle_path: Path | None
    request_timeout_seconds: float
    retry_attempts: int
    backoff_base_seconds: float
    backoff_max_seconds: float

    mode: ClassVar[str] = "remote"

    def protected_paths(self) -> tuple[Path, ...]:
        """Paths no sandboxed command may read or write.

        Remotely the sensitive local state is not a database but the two
        credentials: the enrollment secret and the cached bearer token.
        """
        return (self.enrollment_secret_path, self.token_cache_path)


TransportConfig = LocalTransportConfig | RemoteTransportConfig


@dataclass(frozen=True)
class WorkerConfig:
    """The validated, closed worker configuration."""

    worker_id: str
    transport: TransportConfig
    worktree_root: Path
    checkpoint_root: Path
    kill_switch_path: Path
    #: Advisory in remote mode: it paces this worker's heartbeats, while the
    #: authoritative lease duration and attempt budget belong to the service.
    lease_ttl_seconds: int
    max_attempts: int
    repos: dict[str, RepoAllowlistEntry]
    #: Optional owner-only file holding the claude -> CCR appkey (DAL-R07B).
    #: Present only when the worker may run the real-coder route.
    coder_token_path: Path | None = None


def load_worker_config(path: Path) -> WorkerConfig:
    """Load and validate the worker config; raises on any unknown field."""
    body = json.loads(path.read_text("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("worker config must be a JSON object")
    # Version first: a 1.0 config's `database_path` is an unknown key here, and
    # reporting it as one would send the reader hunting for a typo instead of
    # telling them the shape changed.
    if body.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError(
            f"unsupported worker config schema: {body.get('schema_version')!r}; "
            f"expected {CONFIG_SCHEMA} with an explicit transport"
        )
    unknown = set(body) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown worker config keys: {sorted(unknown)!r}")

    worker_id = body.get("worker_id")
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("worker_id must be a non-empty string")

    transport = _transport_field(body)
    worktree_root = _path_field(body, "worktree_root")
    checkpoint_root = _path_field(body, "checkpoint_root")
    kill_switch_path = _path_field(body, "kill_switch_path")

    lease_ttl_seconds = _positive_int(body, "lease_ttl_seconds")
    max_attempts = _positive_int(body, "max_attempts")

    raw_repos = body.get("repos")
    if not isinstance(raw_repos, dict) or not raw_repos:
        raise ValueError("repos must be a non-empty object")
    repos: dict[str, RepoAllowlistEntry] = {}
    for repository_id, entry in raw_repos.items():
        if not isinstance(entry, dict):
            raise ValueError(f"repo {repository_id!r} must be an object")
        unknown_repo = set(entry) - _REPO_KEYS
        if unknown_repo:
            raise ValueError(
                f"unknown repo {repository_id!r} keys: {sorted(unknown_repo)!r}"
            )
        local_path = entry.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            raise ValueError(f"repo {repository_id!r} local_path must be a non-empty string")
        repos[repository_id] = RepoAllowlistEntry(
            repository_id=repository_id, local_path=str(Path(local_path).resolve())
        )

    coder_token_path = body.get("coder_token_path")
    if coder_token_path is not None:
        coder_token_path = _path_field(body, "coder_token_path")

    return WorkerConfig(
        worker_id=worker_id,
        transport=transport,
        worktree_root=worktree_root,
        checkpoint_root=checkpoint_root,
        kill_switch_path=kill_switch_path,
        lease_ttl_seconds=lease_ttl_seconds,
        max_attempts=max_attempts,
        repos=repos,
        coder_token_path=coder_token_path,
    )


def _transport_field(body: dict) -> TransportConfig:
    """Validate the closed transport union.

    The two modes have disjoint key sets, so naming a database in remote mode —
    or an endpoint in local mode — is an unknown key, and refused by name.
    """
    transport = body.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("transport must be an object")
    mode = transport.get("mode")
    if mode == "local":
        unknown = set(transport) - _LOCAL_KEYS
        if unknown:
            raise ValueError(f"unknown local transport keys: {sorted(unknown)!r}")
        return LocalTransportConfig(database_path=_path_field(transport, "database_path"))
    if mode == "remote":
        unknown = set(transport) - (_REMOTE_REQUIRED | _REMOTE_OPTIONAL)
        if unknown:
            raise ValueError(f"unknown remote transport keys: {sorted(unknown)!r}")
        missing = _REMOTE_REQUIRED - set(transport)
        if missing:
            raise ValueError(f"missing remote transport keys: {sorted(missing)!r}")
        endpoint = transport.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a non-empty string")
        try:
            pinned = validate_endpoint(endpoint)
        except EndpointError as error:
            raise ValueError(f"invalid endpoint: {error}") from error
        machine_id = transport.get("machine_id")
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError("machine_id must be a non-empty string")
        capabilities = _capabilities(transport)
        ca_bundle = transport.get("ca_bundle_path")
        if ca_bundle is not None and (not isinstance(ca_bundle, str) or not ca_bundle):
            raise ValueError("ca_bundle_path must be a non-empty string when present")
        return RemoteTransportConfig(
            endpoint=pinned,
            machine_id=machine_id,
            capabilities=capabilities,
            enrollment_secret_path=_path_field(transport, "enrollment_secret_path"),
            token_cache_path=_path_field(transport, "token_cache_path"),
            ca_bundle_path=Path(ca_bundle).resolve() if ca_bundle else None,
            request_timeout_seconds=_positive_number(
                transport,
                "request_timeout_seconds",
                _DEFAULT_TIMEOUT_SECONDS,
                _MAX_TIMEOUT_SECONDS,
            ),
            retry_attempts=_bounded_int(
                transport, "retry_attempts", _DEFAULT_RETRY_ATTEMPTS, _MAX_RETRY_ATTEMPTS
            ),
            backoff_base_seconds=_positive_number(
                transport,
                "backoff_base_seconds",
                _DEFAULT_BACKOFF_BASE_SECONDS,
                _MAX_BACKOFF_BASE_SECONDS,
            ),
            backoff_max_seconds=_positive_number(
                transport,
                "backoff_max_seconds",
                _DEFAULT_BACKOFF_MAX_SECONDS,
                _MAX_BACKOFF_MAX_SECONDS,
            ),
        )
    raise ValueError(f"transport mode must be 'local' or 'remote', not {mode!r}")


def _capabilities(transport: dict) -> tuple[str, ...]:
    """Validate the declared capabilities as a shape only.

    Membership is the service's decision — it answers 403 for a capability it
    does not know — so the worker does not keep a second copy of that list to
    drift out of date.
    """
    capabilities = transport.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("capabilities must be a non-empty array")
    if not all(isinstance(item, str) and item for item in capabilities):
        raise ValueError("capabilities must be non-empty strings")
    if len(set(capabilities)) != len(capabilities):
        raise ValueError("capabilities must not repeat")
    return tuple(capabilities)


def _path_field(body: dict, key: str) -> Path:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    # Resolve to an absolute, canonical path so downstream git/subprocess calls
    # never reinterpret a relative path against a `-C` working directory.
    return Path(value).resolve()


def _positive_int(body: dict, key: str) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _bounded_int(body: dict, key: str, default: int, maximum: int) -> int:
    if key not in body:
        return default
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    if value > maximum:
        raise ValueError(f"{key} must be at most {maximum}")
    return value


def _positive_number(body: dict, key: str, default: float, maximum: float) -> float:
    if key not in body:
        return default
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{key} must be a positive number")
    if value > maximum:
        raise ValueError(f"{key} must be at most {maximum:g}")
    return float(value)
