"""`DAL-032`: the ECS GitHub Control Plane Adapter mechanics (R09-B, part 1).

The ECS backend is the loop's only automatic GitHub writer (技术方案 §3.2):
it pushes the feature branch, opens the PR, and writes the D1 check runs.
This module owns the **mechanics** of that writeship — no policy. The
frozen hard edges (Roadmap R09-B 段) it is responsible for:

- **The App credential never leaves the pinned endpoint.** An installation
  access token is minted from a signed App JWT and travels only to
  ``https://api.github.com`` and the single installation's repo URLs.
  Redirects are refusals, proxy env vars are ignored, TLS stays on.
- **The token is short-lived and per-use.** Each adapter operation mints its
  own JWT from the ECS-held private key; the installation token it buys is
  cached for its stated lifetime minus a skew and never logged, never put
  in a URL, and never returned to a caller.
- **Every response is read back into a closed shape.** A push, PR or check
  write is only a fact once the read-back carries the exact repository,
  head SHA / PR number / check name and the created object's identity.
  Anything else — a drifted field, an unknown shape, a redirect — is a
  refusal, never a repair.
- **Response loss is visible, not guessed.** Where an operation's outcome
  cannot be confirmed (timeout, transport failure after the write may have
  landed), the adapter returns an *unknown* outcome with the request's
  idempotency key; DAL-034's post-read reconciliation owns the recovery,
  and this module never retries a write on its own.

Composition with the store (``adapter_store.py``) is the safety order:
persist intent → claim → dispatch (this module) → confirm or unknown.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import jwt
import httpx

from personal_agent_core.timeutil import utc_now

#: The only API host a credential may travel to (§5.1: pin the endpoint).
GITHUB_API_HOST: Final[str] = "api.github.com"
GITHUB_API_BASE: Final[str] = f"https://{GITHUB_API_HOST}"

#: Refresh an installation token this many seconds before it expires, so a
#: write in flight cannot cross the expiry boundary.
TOKEN_REFRESH_SKEW_SECONDS: Final[int] = 60

#: App JWT lifetime: GitHub caps these at 10 minutes; we ask for less so a
#: clock skew on either side cannot extend validity.
APP_JWT_TTL_SECONDS: Final[int] = 8 * 60

SCHEMA_VERSION: Final[str] = "dal.github-adapter/1.0"


class AdapterError(RuntimeError):
    """A pinned-shape violation: the transport or read-back failed closed."""


@dataclass(frozen=True)
class AdapterRefusal:
    """A mechanical refusal, labelled with *when* it happened.

    The stage is the safety fact the composition layer judges, so the judge
    never parses prose to decide a lifecycle edge:

    - ``pre_write`` — nothing was sent (argument validation, key read, token
      mint): not-executed is provable.
    - ``write`` — the server definitively refused the write itself (4xx on
      the create call, or a read-back that proves absence after such a
      refusal): not-executed is provable.
    - ``post_write`` — the write may have landed and its disposition is
      unprovable (drift or failure in a read-back after a 201): only the
      composition layer's unknown path may own this. The default, so an
      unlabelled refusal fails closed to unknown rather than fabricating a
      clean not-executed.
    """

    reason: str
    stage: str = "post_write"


@dataclass(frozen=True)
class PushOutcome:
    """The read-back of one branch push, or a refusal/unknown marker."""

    repository_id: str | None
    branch: str | None
    head_sha: str | None
    refusal: AdapterRefusal | None = None
    unknown: bool = False
    idempotency_key: str | None = None


@dataclass(frozen=True)
class PullRequestOutcome:
    """The read-back of one PR creation, or a refusal/unknown marker."""

    repository_id: str | None
    pull_request_number: int | None
    head_sha: str | None
    refusal: AdapterRefusal | None = None
    unknown: bool = False
    idempotency_key: str | None = None


@dataclass(frozen=True)
class CheckRunOutcome:
    """The read-back of one check-run write, or a refusal/unknown marker."""

    repository_id: str | None
    check_name: str | None
    head_sha: str | None
    check_run_id: int | None
    external_id: str | None
    conclusion: str | None
    refusal: AdapterRefusal | None = None
    unknown: bool = False
    idempotency_key: str | None = None


@dataclass(frozen=True)
class BranchReadBack:
    """One authoritative branch read (DAL-034 reconciliation).

    ``found`` is tri-state: ``True`` (exists at ``head_sha``), ``False``
    (the server proves absence with a 404), ``None`` (unknowable — the
    response shape is wrong or the transport failed). A drifted head SHA
    reads as ``None``-found with a drift refusal, never as a confirmation.
    """

    found: bool | None
    head_sha: str | None = None
    unknown: bool = False


@dataclass(frozen=True)
class OpenPullRequestsReadBack:
    """One authoritative open-PR read for an exact head/base pair.

    ``matches`` counts only PRs whose head branch, base branch, repository
    and open state are exact; a PR naming another branch is not a match.
    """

    matches: int
    pull_request_number: int | None = None
    head_sha: str | None = None
    unknown: bool = False


@dataclass(frozen=True)
class CheckRunReadBack:
    """One authoritative check-run read for an exact SHA/name/external_id."""

    found: bool | None
    check_run_id: int | None = None
    head_sha: str | None = None
    unknown: bool = False


@dataclass(frozen=True)
class GithubAdapterSettings:
    """Pinned, validated adapter configuration.

    ``app_id`` is the numeric App id GitHub assigned; ``private_key_path``
    points at the ECS-held installation key (root-owned, group 0640);
    ``repository`` is the single sandbox repo the installation covers, as
    ``owner/name``. Any drift in these at construction time is a hard
    failure: the adapter never discovers its own authority.
    """

    app_id: str
    private_key_path: Path
    repository: str
    api_base: str = GITHUB_API_BASE
    request_timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if type(self.app_id) is not str or not self.app_id.isdigit():
            raise AdapterError("app_id must be the numeric GitHub App id")
        split = urlsplit(self.api_base)
        if split.scheme != "https" or split.hostname != GITHUB_API_HOST:
            raise AdapterError(
                f"api_base must be https://{GITHUB_API_HOST}, not {self.api_base!r}"
            )
        if split.path not in ("", "/"):
            raise AdapterError("api_base must not carry a path")
        owner, sep, name = self.repository.partition("/")
        if not sep or not owner or not name or "/" in name:
            raise AdapterError(
                "repository must be 'owner/name', got "
                f"{self.repository!r}"
            )


def _repo_url(settings: GithubAdapterSettings) -> str:
    return f"{settings.api_base}/repos/{settings.repository}"


# --- the App JWT / installation token exchange -------------------------------


def _load_private_key(settings: GithubAdapterSettings) -> str:
    """Read the App private key from its owner-only file, at use time.

    The key lives only on the ECS (root:personal-agent-dal 0640, 密钥清单);
    it is never cached in the adapter, never logged, and never leaves the
    process except inside the signed JWT sent to the pinned token endpoint.
    A missing or unreadable key is a refusal, not an empty-signer fallback.
    """
    path = settings.private_key_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise AdapterError(f"app private key unreadable: {error}") from error
    if "-----BEGIN" not in text or "PRIVATE KEY-----" not in text:
        raise AdapterError("app private key file is not a PEM private key")
    return text


def _app_jwt(settings: GithubAdapterSettings, *, now: Callable[[], int]) -> str:
    """Sign the one App JWT this token mint needs (RS256, short TTL)."""
    issued = now()
    payload = {"iat": issued - 30, "exp": issued + APP_JWT_TTL_SECONDS, "iss": settings.app_id}
    return jwt.encode(
        payload,
        _load_private_key(settings),
        algorithm="RS256",
    )


@dataclass(frozen=True)
class _InstallationToken:
    token: str
    expires_at_epoch: int


def _parse_github_date(value: str) -> int | None:
    from datetime import datetime

    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return int(parsed.timestamp())


def _mint_installation_token(
    settings: GithubAdapterSettings,
    *,
    transport: httpx.Client,
    now: Callable[[], int],
) -> _InstallationToken | AdapterRefusal:
    """Exchange the App JWT for one installation access token."""
    token = _app_jwt(settings, now=now)
    response = transport.post(
        f"{settings.api_base}/app/installations/{settings.app_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if response.status_code != 201:
        return AdapterRefusal(
            f"installation token mint failed: HTTP {response.status_code}",
            stage="pre_write",
        )
    try:
        body = response.json()
    except ValueError:
        return AdapterRefusal(
            "installation token mint returned a non-JSON body", stage="pre_write"
        )
    token_value = body.get("token")
    expires_at = _parse_github_date(body.get("expires_at", ""))
    if not isinstance(token_value, str) or not token_value or expires_at is None:
        return AdapterRefusal(
            "installation token mint returned an unusable shape", stage="pre_write"
        )
    return _InstallationToken(token=token_value, expires_at_epoch=expires_at)


# --- the closed response shapes ----------------------------------------------


def _closed_field(body: dict[str, Any], key: str, kinds: tuple[type, ...]) -> Any | None:
    value = body.get(key)
    if isinstance(value, kinds) and value is not True and value is not False:
        return value
    return None


def _validate_push_readback(
    body: Any, *, repository_id: str, branch: str
) -> PushOutcome:
    """Judge a GET /repos/{repo}/git/ref/heads/{branch} read-back."""
    if not isinstance(body, dict):
        return PushOutcome(None, None, None, refusal=AdapterRefusal("push read-back is not an object"))
    ref = body.get("ref")
    obj = body.get("object")
    if ref != f"refs/heads/{branch}" or not isinstance(obj, dict):
        return PushOutcome(None, None, None, refusal=AdapterRefusal("push read-back carries a drifted ref"))
    head_sha = obj.get("sha")
    if not isinstance(head_sha, str) or len(head_sha) != 40:
        return PushOutcome(None, None, None, refusal=AdapterRefusal("push read-back carries no head SHA"))
    return PushOutcome(repository_id=repository_id, branch=branch, head_sha=head_sha)


def _validate_pr_readback(
    body: Any, *, repository_id: str, head_branch: str, base_branch: str
) -> PullRequestOutcome:
    """Judge a POST /pulls response: exact repo, head and base, open state."""
    if not isinstance(body, dict):
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back is not an object"))
    number = body.get("number")
    head = body.get("head")
    base = body.get("base")
    state = body.get("state")
    if not isinstance(number, int) or number <= 0:
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back carries no number"))
    if state != "open":
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back is not open"))
    if not isinstance(head, dict) or not isinstance(base, dict):
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back lacks head/base"))
    if head.get("ref") != head_branch or base.get("ref") != base_branch:
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back branches drifted"))
    repo = head.get("repo")
    if not isinstance(repo, dict) or repo.get("full_name") != repository_id:
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back names another repository"))
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or len(head_sha) != 40:
        return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back carries no head SHA"))
    return PullRequestOutcome(repository_id=repository_id, pull_request_number=number, head_sha=head_sha)


def _validate_check_readback(
    body: Any, *, repository_id: str, check_name: str, head_sha: str, external_id: str
) -> CheckRunOutcome:
    """Judge a POST/GET check-runs response: exact SHA, name, external id."""
    if not isinstance(body, dict):
        return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back is not an object"))
    name = body.get("name")
    check_sha = body.get("head_sha")
    external = body.get("external_id")
    check_id = body.get("id")
    if name != check_name:
        return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back name drifted"))
    if check_sha != head_sha:
        return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back head SHA drifted"))
    if external != external_id:
        return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back external id drifted"))
    if not isinstance(check_id, int) or check_id <= 0:
        return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back carries no id"))
    conclusion = body.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back conclusion unusable"))
    return CheckRunOutcome(
        repository_id=repository_id,
        check_name=check_name,
        head_sha=head_sha,
        check_run_id=check_id,
        external_id=external_id,
        conclusion=conclusion,
    )


# --- the adapter -------------------------------------------------------------


class GithubAdapter:
    """The single automatic GitHub writer's mechanics.

    Production builds its own pinned ``httpx.Client``; tests inject one.
    The production construction cannot be skipped by a test, because
    production passes no client.
    """

    def __init__(
        self,
        settings: GithubAdapterSettings,
        *,
        client: httpx.Client | None = None,
        now_epoch: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self._settings = settings
        self._now_epoch = now_epoch
        self._owns_client = client is None
        self._client = client or httpx.Client(
            verify=True,
            follow_redirects=False,
            timeout=settings.request_timeout_seconds,
            # A tampered HTTPS_PROXY / *_PROXY env var must not reroute the
            # credential (CLAUDE.md §5.1): trust_env=False ignores them.
            trust_env=False,
        )
        self._cached_token: _InstallationToken | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GithubAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- credential plumbing -------------------------------------------------

    def _installation_token(self) -> str | AdapterRefusal:
        cached = self._cached_token
        if cached is not None and cached.expires_at_epoch - TOKEN_REFRESH_SKEW_SECONDS > self._now_epoch():
            return cached.token
        try:
            minted = _mint_installation_token(self._settings, transport=self._client, now=self._now_epoch)
        except httpx.HTTPError:
            # Credential mint loss is a transport failure like any other: the
            # caller's read/write path judges it fail-closed, not a crash.
            return AdapterRefusal("installation token mint lost in transit", stage="pre_write")
        if isinstance(minted, AdapterRefusal):
            return minted
        self._cached_token = minted
        return minted.token

    def _headers(self) -> dict[str, str] | AdapterRefusal:
        token = self._installation_token()
        if isinstance(token, AdapterRefusal):
            return token
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    # --- the three writes -----------------------------------------------------

    def push_feature_branch(
        self,
        *,
        branch: str,
        head_sha: str,
        idempotency_key: str,
    ) -> PushOutcome:
        """Ensure the feature branch exists at ``head_sha`` on the remote.

        The write is the create-ref call; the read-back is the separate
        ref GET. A create-ref response alone is never a fact.
        """
        if not _valid_branch_name(branch):
            return PushOutcome(
                None, None, None,
                refusal=AdapterRefusal("branch name is not a valid feature branch", stage="pre_write"),
            )
        if not _valid_sha(head_sha):
            return PushOutcome(
                None, None, None,
                refusal=AdapterRefusal("head SHA is not a 40-hex object id", stage="pre_write"),
            )
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return PushOutcome(None, None, None, refusal=headers)
        repository_id = self._settings.repository
        try:
            create = self._client.post(
                f"{_repo_url(self._settings)}/git/refs",
                headers=headers,
                json={"ref": f"refs/heads/{branch}", "sha": head_sha},
            )
        except httpx.HTTPError:
            return PushOutcome(
                None, None, None, unknown=True, idempotency_key=idempotency_key
            )
        if create.status_code == 422:
            # Either the branch exists or the SHA does not. Only the
            # read-back decides: get the ref and compare head SHAs.
            return self._push_readback(branch=branch, head_sha=head_sha, repository_id=repository_id, idempotency_key=idempotency_key)
        if create.status_code != 201:
            return PushOutcome(
                None, None, None,
                refusal=AdapterRefusal(f"branch create refused: HTTP {create.status_code}", stage="write"),
            )
        return self._push_readback(branch=branch, head_sha=head_sha, repository_id=repository_id, idempotency_key=idempotency_key)

    def _push_readback(
        self, *, branch: str, head_sha: str, repository_id: str, idempotency_key: str
    ) -> PushOutcome:
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return PushOutcome(None, None, None, refusal=headers)
        try:
            read = self._client.get(
                f"{_repo_url(self._settings)}/git/ref/heads/{branch}",
                headers=headers,
            )
        except httpx.HTTPError:
            return PushOutcome(None, None, None, unknown=True, idempotency_key=idempotency_key)
        if read.status_code != 200:
            return PushOutcome(None, None, None, refusal=AdapterRefusal(f"push read-back refused: HTTP {read.status_code}"))
        try:
            body = read.json()
        except ValueError:
            return PushOutcome(None, None, None, refusal=AdapterRefusal("push read-back is not JSON"))
        judged = _validate_push_readback(body, repository_id=repository_id, branch=branch)
        if judged.refusal is not None or judged.head_sha != head_sha:
            if judged.refusal is None:
                judged = PushOutcome(
                    repository_id, branch, None,
                    refusal=AdapterRefusal("push read-back head SHA drifted from the written SHA"),
                )
            return judged
        return judged

    def create_pull_request(
        self,
        *,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> PullRequestOutcome:
        """Create the feature PR and read it back."""
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return PullRequestOutcome(None, None, None, refusal=headers)
        repository_id = self._settings.repository
        try:
            response = self._client.post(
                f"{_repo_url(self._settings)}/pulls",
                headers=headers,
                json={"title": title, "body": body, "head": branch, "base": base_branch},
            )
        except httpx.HTTPError:
            return PullRequestOutcome(
                None, None, None, unknown=True, idempotency_key=idempotency_key
            )
        if response.status_code == 422:
            # A PR for this head may already exist (a replayed create). The
            # existing object is the fact: list open PRs for this head and
            # read it back with the same closed shape.
            return self._existing_pr_readback(
                branch=branch, base_branch=base_branch, repository_id=repository_id,
                idempotency_key=idempotency_key,
            )
        if response.status_code != 201:
            return PullRequestOutcome(
                None, None, None,
                refusal=AdapterRefusal(f"PR create refused: HTTP {response.status_code}", stage="write"),
            )
        try:
            payload = response.json()
        except ValueError:
            return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR read-back is not JSON"))
        judged = _validate_pr_readback(payload, repository_id=repository_id, head_branch=branch, base_branch=base_branch)
        return judged

    def _existing_pr_readback(
        self, *, branch: str, base_branch: str, repository_id: str, idempotency_key: str
    ) -> PullRequestOutcome:
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return PullRequestOutcome(None, None, None, refusal=headers)
        try:
            listing = self._client.get(
                f"{_repo_url(self._settings)}/pulls",
                headers=headers,
                params={"head": f"{repository_id.split('/')[0]}:{branch}", "base": base_branch, "state": "open"},
            )
        except httpx.HTTPError:
            return PullRequestOutcome(None, None, None, unknown=True, idempotency_key=idempotency_key)
        if listing.status_code != 200:
            return PullRequestOutcome(None, None, None, refusal=AdapterRefusal(f"PR listing refused: HTTP {listing.status_code}"))
        try:
            items = listing.json()
        except ValueError:
            return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR listing is not JSON"))
        if not isinstance(items, list) or len(items) != 1:
            return PullRequestOutcome(None, None, None, refusal=AdapterRefusal("PR listing did not name exactly one open PR"))
        judged = _validate_pr_readback(items[0], repository_id=repository_id, head_branch=branch, base_branch=base_branch)
        return judged

    def write_check_run(
        self,
        *,
        branch_head_sha: str,
        check_name: str,
        external_id: str,
        conclusion: str,
        details_url: str | None,
        idempotency_key: str,
    ) -> CheckRunOutcome:
        """Create one D1 check run for the exact head SHA.

        D1 mode (DAL-033): the run's name, SHA and external_id are the
        binding; the read-back must carry all three plus the run id, or the
        write is not a fact.
        """
        if not _valid_sha(branch_head_sha):
            return CheckRunOutcome(
                None, None, None, None, None, None,
                refusal=AdapterRefusal("check head SHA is not a 40-hex object id", stage="pre_write"),
            )
        if not check_name or not isinstance(check_name, str):
            return CheckRunOutcome(
                None, None, None, None, None, None,
                refusal=AdapterRefusal("check name is empty", stage="pre_write"),
            )
        if external_id and not isinstance(external_id, str):
            return CheckRunOutcome(
                None, None, None, None, None, None,
                refusal=AdapterRefusal("check external id is not a string", stage="pre_write"),
            )
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return CheckRunOutcome(None, None, None, None, None, None, refusal=headers)
        repository_id = self._settings.repository
        payload: dict[str, Any] = {
            "name": check_name,
            "head_sha": branch_head_sha,
            "status": "completed",
            "conclusion": conclusion,
            "external_id": external_id,
        }
        if details_url is not None:
            payload["details_url"] = details_url
        try:
            response = self._client.post(
                f"{_repo_url(self._settings)}/check-runs",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError:
            return CheckRunOutcome(
                None, None, None, None, None, None,
                unknown=True, idempotency_key=idempotency_key,
            )
        if response.status_code != 201:
            return CheckRunOutcome(
                None, None, None, None, None, None,
                refusal=AdapterRefusal(f"check create refused: HTTP {response.status_code}", stage="write"),
            )
        try:
            body = response.json()
        except ValueError:
            return CheckRunOutcome(None, None, None, None, None, None, refusal=AdapterRefusal("check read-back is not JSON"))
        judged = _validate_check_readback(
            body, repository_id=repository_id, check_name=check_name,
            head_sha=branch_head_sha, external_id=external_id,
        )
        return judged

    # --- the three authoritative read-backs (DAL-034 reconciliation) ------
    #
    # GET-only by construction: a recovery read must not be able to become a
    # duplicate write. Every method judges its response against the *exact*
    # target identity and fails closed to the unknown shape.

    def read_feature_branch(self, *, branch: str) -> BranchReadBack:
        """Does the branch exist, and at which head SHA? (GET ref only.)"""
        if not _valid_branch_name(branch):
            return BranchReadBack(found=None, unknown=True)
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return BranchReadBack(found=None, unknown=True)
        try:
            read = self._client.get(
                f"{_repo_url(self._settings)}/git/ref/heads/{branch}",
                headers=headers,
            )
        except httpx.HTTPError:
            return BranchReadBack(found=None, unknown=True)
        if read.status_code == 404:
            return BranchReadBack(found=False)
        if read.status_code != 200:
            return BranchReadBack(found=None, unknown=True)
        try:
            body = read.json()
        except ValueError:
            return BranchReadBack(found=None, unknown=True)
        if not isinstance(body, dict):
            return BranchReadBack(found=None, unknown=True)
        obj = body.get("object")
        if body.get("ref") != f"refs/heads/{branch}" or not isinstance(obj, dict):
            return BranchReadBack(found=None, unknown=True)
        head_sha = obj.get("sha")
        if not isinstance(head_sha, str) or len(head_sha) != 40:
            return BranchReadBack(found=None, unknown=True)
        return BranchReadBack(found=True, head_sha=head_sha)

    def list_open_pull_requests(
        self, *, branch: str, base_branch: str
    ) -> OpenPullRequestsReadBack:
        """How many open PRs does this exact head/base pair have?"""
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return OpenPullRequestsReadBack(0, unknown=True)
        repository_id = self._settings.repository
        try:
            listing = self._client.get(
                f"{_repo_url(self._settings)}/pulls",
                headers=headers,
                params={
                    "head": f"{repository_id.split('/')[0]}:{branch}",
                    "base": base_branch,
                    "state": "open",
                },
            )
        except httpx.HTTPError:
            return OpenPullRequestsReadBack(0, unknown=True)
        if listing.status_code != 200:
            return OpenPullRequestsReadBack(0, unknown=True)
        try:
            items = listing.json()
        except ValueError:
            return OpenPullRequestsReadBack(0, unknown=True)
        if not isinstance(items, list):
            return OpenPullRequestsReadBack(0, unknown=True)
        matches = 0
        number: int | None = None
        head_sha: str | None = None
        for item in items:
            judged = _validate_pr_readback(
                item, repository_id=repository_id,
                head_branch=branch, base_branch=base_branch,
            )
            if judged.refusal is None:
                matches += 1
                number, head_sha = judged.pull_request_number, judged.head_sha
        return OpenPullRequestsReadBack(matches, pull_request_number=number, head_sha=head_sha)

    def read_check_run(
        self, *, branch_head_sha: str, check_name: str, external_id: str
    ) -> CheckRunReadBack:
        """Does a check run exist for this exact SHA/name/external id?"""
        headers = self._headers()
        if isinstance(headers, AdapterRefusal):
            return CheckRunReadBack(found=None, unknown=True)
        try:
            listing = self._client.get(
                f"{_repo_url(self._settings)}/commits/{branch_head_sha}/check-runs",
                headers=headers,
                params={"check_name": check_name, "filter": "latest"},
            )
        except httpx.HTTPError:
            return CheckRunReadBack(found=None, unknown=True)
        if listing.status_code == 404:
            # The SHA itself is absent: the run cannot exist either.
            return CheckRunReadBack(found=False)
        if listing.status_code != 200:
            return CheckRunReadBack(found=None, unknown=True)
        try:
            body = listing.json()
        except ValueError:
            return CheckRunReadBack(found=None, unknown=True)
        if not isinstance(body, dict) or not isinstance(body.get("check_runs"), list):
            return CheckRunReadBack(found=None, unknown=True)
        for item in body["check_runs"]:
            judged = _validate_check_readback(
                item, repository_id=self._settings.repository,
                check_name=check_name, head_sha=branch_head_sha,
                external_id=external_id,
            )
            if judged.refusal is None:
                return CheckRunReadBack(
                    found=True, check_run_id=judged.check_run_id,
                    head_sha=judged.head_sha,
                )
        return CheckRunReadBack(found=False)


def _valid_branch_name(branch: str) -> bool:
    """A feature branch under our naming rule: no whitespace, no ref tricks.

    git refnames cannot begin with ``.``, contain ``..``, ``@{``, ASCII
    control chars, ``~ ^ : ? * [ \\ `` or end with ``.lock`` or ``/``. The
    adapter enforces the mechanics; the policy layer restricts to the
    feature prefix.
    """
    if not branch or not isinstance(branch, str):
        return False
    if branch.startswith(".") or branch.startswith("/") or branch.endswith("/"):
        return False
    if branch.endswith(".lock") or ".." in branch or "@{" in branch:
        return False
    forbidden = set(" ~^:?*[\\\x7f") | {chr(i) for i in range(0x20)}
    if any(ch in forbidden for ch in branch):
        return False
    return all(part != "" for part in branch.split("/"))


def _valid_sha(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(ch in "0123456789abcdef" for ch in value)
    )


def sha256_hex(data: bytes) -> str:
    """The digest a caller uses to bind request payloads into evidence."""
    return hashlib.sha256(data).hexdigest()
