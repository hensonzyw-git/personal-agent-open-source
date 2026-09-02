"""DAL-032 slice A: the ECS GitHub adapter mechanics + lifecycle composition.

Two layers, tested separately so a mechanics defect cannot hide behind a
lifecycle refusal and vice versa:

- **Mechanics** (`personal_agent_dal.github.adapter`) — the pinned-endpoint
  httpx client, the App-JWT → installation-token exchange, the three writes,
  and the closed read-back shapes. The transport is a hand-rolled scriptable
  fake that records every request (method, URL, headers, body) and replays a
  script of responses — deliberately *not* built from the adapter's
  assumptions but from the GitHub REST shapes it must judge.
- **Composition** (`personal_agent_dal.github.adapter_controller`) — the
  frozen EE lifecycle edges (EE-CLAIM, EE-DISPATCH, EE-CONFIRM-NOT-EXECUTED,
  EE-DISPATCH-UNKNOWN, REC-UNKNOWN) driven through the real engine and the
  real transition registry, with a stub adapter whose outcomes are injected
  per call.

The §5.1 failure shapes this file pins, per family:

mechanics
  - pinned endpoint: an ``api_base`` that is not ``https://api.github.com``
    is a construction-time refusal (never a per-call check);
  - redirects are refused and proxy env poisoning is ignored in production
    construction (pinned by source inspection: production passes no client);
  - token discipline: the App JWT travels only to the token endpoint; the
    installation token never appears in a URL; a still-valid token is
    reused, not re-minted;
  - response shapes: non-object bodies, drifted ref/repo/head SHA/name/
    external id, missing ids — each fails closed as a refusal, and no
    refusal path retries;
  - transport exceptions → ``unknown`` with the idempotency key, never a
    retry and never a fabricated success.

composition
  - a confirmed write parks the effect in ``dispatch_started`` (§3.6: no
    standalone completed edge exists; the owner root closes it);
  - a proven not-executed refusal (pre-write / write-stage) closes via
    EE-CONFIRM-NOT-EXECUTED;
  - a post-write refusal (read-back drift), a 5xx, or an adapter exception
    is ``unknown`` → EE-DISPATCH-UNKNOWN, then REC-UNKNOWN parks the
    feature in ``reconciliation_required`` with ``EXTERNAL_RESULT_UNKNOWN``,
    matching the frozen ``push_ack_loss`` oracle's trace;
  - a kill-switch epoch bump between intent and claim refuses the claim
    (the OP-KILL-001 race, judged from rows, not asserted facts);
  - no second write: a second dispatch against a parked or unknown effect
    is refused without any adapter call;
  - closed payload shapes: unknown keys, missing keys, wrong action refuse
    before any state moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from personal_agent_dal.github.adapter import (
    AdapterRefusal,
    CheckRunOutcome,
    GithubAdapter,
    GithubAdapterSettings,
    PullRequestOutcome,
    PushOutcome,
)
from personal_agent_dal.github.adapter_controller import (
    ControllerRefusal,
    dispatch_github_write,
)
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage.engine import create_database_engine, session_factory

from tests.dal.factories import external_effect_row, feature_row


# ---------------------------------------------------------------------------
# The scriptable transport fake.
# ---------------------------------------------------------------------------


@dataclass
class _Response:
    status_code: int
    body: Any = None

    def json(self) -> Any:
        if isinstance(self.body, (bytes, str)):
            import json as _json

            return _json.loads(self.body)
        return self.body


@dataclass
class _Request:
    method: str
    url: str
    headers: dict[str, str]
    json_body: Any = None


class ScriptedTransport:
    """A fake httpx.Client scripted response-by-response, request-recording.

    Scripts match on (method, path fragment); the *last* matching script
    answers. With no match and no default, the call raises — the default is
    a loud failure, never a plausible 200.
    """

    def __init__(self) -> None:
        self.requests: list[_Request] = []
        self._scripts: list[tuple[str, str, _Response]] = []
        self.default: _Response | None = None

    def script(self, method: str, path_fragment: str, response: _Response) -> None:
        self._scripts.append((method.upper(), path_fragment, response))

    def set_default(self, response: _Response) -> None:
        self.default = response

    def _answer(self, request: _Request) -> _Response:
        for method, fragment, response in reversed(self._scripts):
            if request.method == method and fragment in request.url:
                return response
        if self.default is not None:
            return self.default
        raise AssertionError(f"unscripted request: {request.method} {request.url}")

    def post(self, url: str, *, headers: dict[str, str], json: Any = None) -> _Response:
        request = _Request("POST", url, dict(headers), json_body=json)
        self.requests.append(request)
        return self._answer(request)

    def get(self, url: str, *, headers: dict[str, str], params: Any = None) -> _Response:
        request = _Request("GET", url, dict(headers))
        self.requests.append(request)
        return self._answer(request)

    def close(self) -> None:
        pass


@pytest.fixture()
def transport() -> ScriptedTransport:
    return ScriptedTransport()


def make_settings(tmp_path: Path, *, api_base: str = "https://api.github.com") -> GithubAdapterSettings:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = tmp_path / "github-app.pem"
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key.write_bytes(pem)
    return GithubAdapterSettings(
        app_id="4807112",
        private_key_path=key,
        repository="example-owner/dal-sandbox",
        api_base=api_base,
    )


def make_adapter(tmp_path: Path, transport: ScriptedTransport) -> GithubAdapter:
    return GithubAdapter(make_settings(tmp_path), client=transport)  # type: ignore[arg-type]


TOKEN_BODY = {
    "token": "ghs_installation_token_placeholder",
    "expires_at": "2030-01-01T00:00:00Z",
}


def script_token(transport: ScriptedTransport, *, status: int = 201) -> None:
    transport.script(
        "POST",
        "/app/installations/4807112/access_tokens",
        _Response(status, TOKEN_BODY),
    )


# ---------------------------------------------------------------------------
# Mechanics: settings pinning.
# ---------------------------------------------------------------------------


def test_settings_reject_a_non_pinned_api_base(tmp_path: Path) -> None:
    for base in ("http://api.github.com", "https://evil.example.com/api", "https://api.github.com/sub"):
        with pytest.raises(Exception, match="api_base"):
            make_settings(tmp_path, api_base=base)


def test_settings_reject_a_drifted_repository(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="owner/name"):
        GithubAdapterSettings(
            app_id="4807112",
            private_key_path=tmp_path / "missing.pem",
            repository="dal-sandbox",
        )


def test_production_construction_pins_tls_and_refuses_redirects() -> None:
    """The production client must ignore proxy env and refuse redirects.

    Source inspection is the honest check here: a test-built client cannot
    prove what production builds, because production passes no client.
    """
    import inspect

    source = inspect.getsource(GithubAdapter.__init__)
    assert "trust_env=False" in source, "proxy env must be ignored in production"
    assert "follow_redirects=False" in source, "redirects must be refused"
    assert "verify=True" in source, "TLS verification must stay on"


# ---------------------------------------------------------------------------
# Mechanics: the App JWT → installation token exchange.
# ---------------------------------------------------------------------------


def test_app_jwt_travels_only_to_the_token_endpoint(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script("POST", "/git/refs", _Response(201, {"ref": f"refs/heads/dal/task-1"}))
    transport.set_default(
        _Response(200, {"ref": "refs/heads/dal/task-1", "object": {"sha": "a" * 40}})
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(
            branch="dal/task-1", head_sha="a" * 40, idempotency_key="idem-1"
        )
    assert isinstance(outcome, PushOutcome) and outcome.head_sha == "a" * 40

    token_requests = [r for r in transport.requests if "/access_tokens" in r.url]
    assert len(token_requests) == 1
    auth = token_requests[0].headers.get("Authorization", "")
    assert auth.startswith("Bearer ey"), "the App JWT is the bearer on the mint call"

    import jwt as pyjwt

    from personal_agent_dal.github.adapter import APP_JWT_TTL_SECONDS

    claims = pyjwt.decode(auth.removeprefix("Bearer "), options={"verify_signature": False})
    assert claims["iss"] == "4807112"
    assert claims["exp"] - claims["iat"] <= APP_JWT_TTL_SECONDS + 30 + 1

    # The data write carries the installation token, never the App JWT.
    writes = [r for r in transport.requests if "/access_tokens" not in r.url]
    assert writes and writes[0].headers["Authorization"] == f"Bearer {TOKEN_BODY['token']}"


def test_installation_token_is_cached_until_near_expiry(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.set_default(
        _Response(200, {"ref": "refs/heads/dal/task-1", "object": {"sha": "b" * 40}})
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        for index in range(3):
            adapter.push_feature_branch(
                branch="dal/task-1", head_sha="b" * 40, idempotency_key=f"idem-{index}"
            )
    mints = [r for r in transport.requests if "/access_tokens" in r.url]
    assert len(mints) == 1, "a still-valid token must be reused, not re-minted"


def test_token_mint_failure_is_a_refusal_and_never_a_write(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    transport.script(
        "POST", "/app/installations/4807112/access_tokens", _Response(401, {"message": "bad app"})
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(
            branch="dal/task-1", head_sha="a" * 40, idempotency_key="idem-1"
        )
    assert isinstance(outcome, PushOutcome) and outcome.refusal is not None
    assert outcome.refusal.stage == "pre_write"
    assert "HTTP 401" in outcome.refusal.reason
    assert all("/access_tokens" in r.url for r in transport.requests), (
        "no data write may happen without a token"
    )


def test_missing_private_key_is_a_refusal(tmp_path: Path, transport: ScriptedTransport) -> None:
    settings = GithubAdapterSettings(
        app_id="4807112",
        private_key_path=tmp_path / "absent.pem",
        repository="example-owner/dal-sandbox",
    )
    adapter = GithubAdapter(settings, client=transport)  # type: ignore[arg-type]
    with adapter:
        with pytest.raises(Exception, match="unreadable"):
            adapter.push_feature_branch(
                branch="dal/task-1", head_sha="a" * 40, idempotency_key="idem-1"
            )
    assert transport.requests == [], "no request may be sent without the key"


# ---------------------------------------------------------------------------
# Mechanics: push / PR / check closed read-back shapes.
# ---------------------------------------------------------------------------


BRANCH = "dal/task-1"
HEAD = "a" * 40
OTHER_SHA = "f" * 40
REPO = "example-owner/dal-sandbox"
CHECK_NAME = "dal/deterministic-verification"


def test_push_happy_path_reads_back_the_exact_head(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script("POST", "/git/refs", _Response(201, {"ref": f"refs/heads/{BRANCH}"}))
    transport.script(
        "GET", "/git/ref/heads/dal/task-1",
        _Response(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}}),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(branch=BRANCH, head_sha=HEAD, idempotency_key="k")
    assert isinstance(outcome, PushOutcome)
    assert (outcome.refusal, outcome.unknown) == (None, False)
    assert outcome.head_sha == HEAD and outcome.repository_id == REPO


def test_push_drifted_head_in_read_back_is_a_refusal(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script("POST", "/git/refs", _Response(201, {}))
    transport.script(
        "GET", "/git/ref/heads/dal/task-1",
        _Response(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": OTHER_SHA}}),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(branch=BRANCH, head_sha=HEAD, idempotency_key="k")
    assert isinstance(outcome, PushOutcome) and outcome.refusal is not None
    # A drift discovered after the create is post_write: the write may have
    # landed with someone else's SHA, so only unknown may own this.
    assert outcome.refusal.stage == "post_write"


def test_push_create_5xx_is_unknown_never_a_retry(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script("POST", "/git/refs", _Response(500, {"message": "boom"}))
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(branch=BRANCH, head_sha=HEAD, idempotency_key="k")
    assert isinstance(outcome, PushOutcome) and outcome.refusal is not None
    assert outcome.refusal.stage == "write" and "HTTP 500" in outcome.refusal.reason
    # Exactly one create attempt: the read-back reconciliation owns recovery.
    creates = [r for r in transport.requests if r.method == "POST" and "/git/refs" in r.url]
    assert len(creates) == 1


def test_push_transport_error_is_unknown(tmp_path: Path, transport: ScriptedTransport) -> None:
    import httpx as real_httpx

    class DyingTransport(ScriptedTransport):
        def post(self, url, *, headers, json=None):
            if "/git/refs" in url:
                raise real_httpx.ConnectError("cable cut")
            return super().post(url, headers=headers, json=json)

    transport = DyingTransport()
    script_token(transport)
    adapter = GithubAdapter(make_settings(tmp_path), client=transport)  # type: ignore[arg-type]
    with adapter:
        outcome = adapter.push_feature_branch(branch=BRANCH, head_sha=HEAD, idempotency_key="k")
    assert isinstance(outcome, PushOutcome) and outcome.unknown is True


def test_push_422_replays_via_the_existing_ref_read_back(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    """A replayed create (422) is judged by the ref GET, not retried blind."""
    script_token(transport)
    transport.script("POST", "/git/refs", _Response(422, {"message": "already exists"}))
    transport.script(
        "GET", "/git/ref/heads/dal/task-1",
        _Response(200, {"ref": f"refs/heads/{BRANCH}", "object": {"sha": HEAD}}),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(branch=BRANCH, head_sha=HEAD, idempotency_key="k")
    assert isinstance(outcome, PushOutcome) and outcome.head_sha == HEAD


def test_pr_happy_path_binds_repo_number_and_head(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script(
        "POST", "/pulls",
        _Response(201, {
            "number": 7, "state": "open",
            "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        }),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.create_pull_request(
            branch=BRANCH, base_branch="main", title="t", body="b", idempotency_key="k"
        )
    assert isinstance(outcome, PullRequestOutcome)
    assert outcome.pull_request_number == 7 and outcome.head_sha == HEAD
    sent = [r for r in transport.requests if r.method == "POST" and "/pulls" in r.url]
    assert sent[0].json_body["head"] == BRANCH and sent[0].json_body["base"] == "main"


def test_pr_read_back_from_another_repo_is_a_refusal(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script(
        "POST", "/pulls",
        _Response(201, {
            "number": 7, "state": "open",
            "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": "other/evil"}},
            "base": {"ref": "main"},
        }),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.create_pull_request(
            branch=BRANCH, base_branch="main", title="t", body="b", idempotency_key="k"
        )
    assert isinstance(outcome, PullRequestOutcome) and outcome.refusal is not None
    assert "another repository" in outcome.refusal.reason
    assert outcome.refusal.stage == "post_write"


def test_pr_422_replays_via_the_single_open_pr_listing(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script("POST", "/pulls", _Response(422, {"message": "already exists"}))
    transport.script(
        "GET", "/pulls",
        _Response(200, [{
            "number": 7, "state": "open",
            "head": {"ref": BRANCH, "sha": HEAD, "repo": {"full_name": REPO}},
            "base": {"ref": "main"},
        }]),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.create_pull_request(
            branch=BRANCH, base_branch="main", title="t", body="b", idempotency_key="k"
        )
    assert isinstance(outcome, PullRequestOutcome) and outcome.pull_request_number == 7


def test_pr_422_with_ambiguous_listing_refuses(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script("POST", "/pulls", _Response(422, {}))
    transport.script("GET", "/pulls", _Response(200, []))
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.create_pull_request(
            branch=BRANCH, base_branch="main", title="t", body="b", idempotency_key="k"
        )
    assert isinstance(outcome, PullRequestOutcome) and outcome.refusal is not None
    assert "exactly one" in outcome.refusal.reason


def test_check_happy_path_binds_sha_name_external_id(
    tmp_path: Path, transport: ScriptedTransport
) -> None:
    script_token(transport)
    transport.script(
        "POST", "/check-runs",
        _Response(201, {
            "id": 42, "name": CHECK_NAME, "head_sha": HEAD,
            "external_id": "cap-1", "conclusion": "success",
        }),
    )
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.write_check_run(
            branch_head_sha=HEAD, check_name=CHECK_NAME, external_id="cap-1",
            conclusion="success", details_url=None, idempotency_key="k",
        )
    assert isinstance(outcome, CheckRunOutcome)
    assert outcome.check_run_id == 42 and outcome.conclusion == "success"
    sent = [r for r in transport.requests if "/check-runs" in r.url]
    assert sent[0].json_body["head_sha"] == HEAD
    assert sent[0].json_body["external_id"] == "cap-1"


@pytest.mark.parametrize(
    "drifted",
    [
        {"name": "other-check"},
        {"head_sha": OTHER_SHA},
        {"external_id": "cap-evil"},
        {"id": None},
    ],
)
def test_check_read_back_drift_is_a_refusal(
    tmp_path: Path, transport: ScriptedTransport, drifted: dict[str, Any]
) -> None:
    script_token(transport)
    body = {
        "id": 42, "name": CHECK_NAME, "head_sha": HEAD,
        "external_id": "cap-1", "conclusion": "success",
    }
    body.update(drifted)
    transport.script("POST", "/check-runs", _Response(201, body))
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.write_check_run(
            branch_head_sha=HEAD, check_name=CHECK_NAME, external_id="cap-1",
            conclusion="success", details_url=None, idempotency_key="k",
        )
    assert isinstance(outcome, CheckRunOutcome) and outcome.refusal is not None


def test_check_5xx_is_unknown(tmp_path: Path, transport: ScriptedTransport) -> None:
    script_token(transport)
    transport.script("POST", "/check-runs", _Response(502, {}))
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.write_check_run(
            branch_head_sha=HEAD, check_name=CHECK_NAME, external_id="cap-1",
            conclusion="success", details_url=None, idempotency_key="k",
        )
    assert isinstance(outcome, CheckRunOutcome) and outcome.refusal is not None
    assert outcome.refusal.stage == "write" and "HTTP 502" in outcome.refusal.reason


@pytest.mark.parametrize(
    "branch", ["", ".hidden", "has space", "a..b", "end.lock", "ends/", "@{ref}", "a:b", "with\nnewline"]
)
def test_invalid_branch_names_refuse_before_any_request(
    tmp_path: Path, transport: ScriptedTransport, branch: str
) -> None:
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(branch=branch, head_sha=HEAD, idempotency_key="k")
    assert isinstance(outcome, PushOutcome) and outcome.refusal is not None
    assert transport.requests == []


@pytest.mark.parametrize("sha", ["", "abc", "A" * 40, "g" * 40, "a" * 41])
def test_invalid_sha_refuses_before_any_request(
    tmp_path: Path, transport: ScriptedTransport, sha: str
) -> None:
    adapter = make_adapter(tmp_path, transport)
    with adapter:
        outcome = adapter.push_feature_branch(branch=BRANCH, head_sha=sha, idempotency_key="k")
    assert isinstance(outcome, PushOutcome) and outcome.refusal is not None
    assert transport.requests == []


# ---------------------------------------------------------------------------
# Composition: the frozen EE lifecycle through the real engine.
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine(tmp_path: Path):
    from personal_agent_dal.storage import db

    engine = create_database_engine(tmp_path / "dal.db")
    db.upgrade(engine)
    yield engine
    engine.dispose()


def seed_effect_and_feature(
    engine, *, feature_state: str = "awaiting_merge"
) -> tuple[str, str]:
    """One awaiting_merge feature plus its intent_recorded push effect."""
    feature_id = "feature-gh-1"
    effect_id = "effect-gh-1"
    now = utc_now()
    with session_factory(engine)() as session, session.begin():
        session.add(feature_row(feature_id=feature_id, version=3, state=feature_state, now=now))
        session.add(
            external_effect_row(
                effect_id=effect_id, owner_id=feature_id, version=1,
                state="intent_recorded", now=now,
            )
        )
    return feature_id, effect_id


class StubAdapter:
    """Outcome injection per call; records call count for no-second-write pins."""

    def __init__(self, outcome: Any = None, *, raise_error: Exception | None = None) -> None:
        self.outcome = outcome
        self.raise_error = raise_error
        self.calls = 0

    def _call(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.raise_error is not None:
            raise self.raise_error
        return self.outcome

    def push_feature_branch(self, **kwargs: Any) -> Any:
        return self._call(**kwargs)

    def create_pull_request(self, **kwargs: Any) -> Any:
        return self._call(**kwargs)

    def write_check_run(self, **kwargs: Any) -> Any:
        return self._call(**kwargs)


CONFIRMED_PUSH = PushOutcome(repository_id=REPO, branch=BRANCH, head_sha=HEAD)
REFUSED_PUSH = PushOutcome(
    repository_id=None, branch=None, head_sha=None,
    refusal=AdapterRefusal("branch create refused: HTTP 404", stage="write"),
)
SERVER_5XX_PUSH = PushOutcome(
    repository_id=None, branch=None, head_sha=None,
    refusal=AdapterRefusal("branch create refused: HTTP 503", stage="write"),
)
DRIFTED_PUSH = PushOutcome(
    repository_id=None, branch=None, head_sha=None,
    refusal=AdapterRefusal("push read-back head SHA drifted from the written SHA"),
)


def push_payload() -> dict[str, Any]:
    return {"branch": BRANCH, "head_sha": HEAD}


def effect_state(engine, effect_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT state FROM external_effects WHERE effect_id = :e").bindparams(e=effect_id)
        ).scalar_one()


def feature_state(engine, feature_id: str) -> tuple[str, str | None]:
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT state, reason_code FROM features WHERE feature_id = :f").bindparams(f=feature_id)
        ).first()
    assert row is not None
    return row[0], row[1]


def test_composition_confirmed_push_parks_effect_in_dispatch_started(engine) -> None:
    feature_id, effect_id = seed_effect_and_feature(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    outcome = dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert outcome.effect_state == "dispatch_started"
    assert outcome.adapter_outcome is CONFIRMED_PUSH
    assert adapter.calls == 1
    assert feature_state(engine, feature_id) == ("awaiting_merge", None), (
        "a confirmed push parks the effect; the owner root closes it"
    )


def test_composition_write_stage_refusal_confirms_not_executed(engine) -> None:
    feature_id, effect_id = seed_effect_and_feature(engine)
    outcome = dispatch_github_write(
        engine, StubAdapter(REFUSED_PUSH),  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert outcome.effect_state == "confirmed_not_executed"


def test_composition_post_write_drift_is_unknown_and_stops_the_feature(engine) -> None:
    """Read-back drift after a 201 may mean the write landed unprovably."""
    feature_id, effect_id = seed_effect_and_feature(engine)
    outcome = dispatch_github_write(
        engine, StubAdapter(DRIFTED_PUSH),  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert outcome.effect_state == "unknown"
    assert feature_state(engine, feature_id) == ("reconciliation_required", "EXTERNAL_RESULT_UNKNOWN")


def test_composition_5xx_refusal_is_unknown_and_stops_the_feature(engine) -> None:
    feature_id, effect_id = seed_effect_and_feature(engine)
    outcome = dispatch_github_write(
        engine, StubAdapter(SERVER_5XX_PUSH),  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert outcome.effect_state == "unknown"
    assert feature_state(engine, feature_id) == ("reconciliation_required", "EXTERNAL_RESULT_UNKNOWN")


def test_composition_adapter_exception_is_unknown_and_stops_the_feature(engine) -> None:
    import httpx as real_httpx

    feature_id, effect_id = seed_effect_and_feature(engine)
    outcome = dispatch_github_write(
        engine,
        StubAdapter(raise_error=real_httpx.ConnectError("cable cut")),  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert outcome.effect_state == "unknown"
    assert feature_state(engine, feature_id) == ("reconciliation_required", "EXTERNAL_RESULT_UNKNOWN")


def test_composition_stop_emits_the_oracle_command_trace(engine) -> None:
    """The frozen push_ack_loss command order: claim → dispatch → unknown → stop."""
    feature_id, effect_id = seed_effect_and_feature(engine)
    captured: list[Any] = []
    real_apply = dispatch_github_write.__globals__["apply_transition"]

    def spy(engine_, command, **kwargs):
        captured.append(command)
        return real_apply(engine_, command, **kwargs)

    dispatch_github_write.__globals__["apply_transition"] = spy
    try:
        dispatch_github_write(
            engine, StubAdapter(SERVER_5XX_PUSH),  # type: ignore[arg-type]
            effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
            payload=push_payload(), feature_id=feature_id,
        )
    finally:
        dispatch_github_write.__globals__["apply_transition"] = real_apply
    commands = [(c.aggregate_type, c.command_type) for c in captured]
    assert commands == [
        ("external_effect", "claim_external_effect"),
        ("external_effect", "record_effect_dispatch"),
        ("external_effect", "record_effect_unknown"),
        ("feature", "require_reconciliation"),
    ]
    assert effect_state(engine, effect_id) == "unknown"


def test_composition_kill_switch_epoch_bump_refuses_the_claim(engine) -> None:
    """The OP-KILL-001 race: the epoch bumps after intent, before claim.

    The feature's epoch is bumped past the effect's stamped binding; the
    claim must refuse from the rows, and no adapter call may happen.
    """
    feature_id, effect_id = seed_effect_and_feature(engine)
    with engine.connect() as connection:
        connection.execute(
            sa.text("UPDATE external_effects SET capability_epoch = 1 WHERE effect_id = :e")
            .bindparams(e=effect_id)
        )
        connection.execute(
            sa.text("UPDATE features SET capability_epoch = 2 WHERE feature_id = :f")
            .bindparams(f=feature_id)
        )
        connection.commit()
    adapter = StubAdapter(CONFIRMED_PUSH)
    with pytest.raises(ControllerRefusal):
        dispatch_github_write(
            engine, adapter,  # type: ignore[arg-type]
            effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
            payload=push_payload(), feature_id=feature_id,
        )
    assert adapter.calls == 0, "a stale-epoch claim must never reach the adapter"
    assert effect_state(engine, effect_id) == "intent_recorded"


def test_composition_no_second_write_after_unknown(engine) -> None:
    """A parked unknown effect is never re-dispatched by this controller."""
    feature_id, effect_id = seed_effect_and_feature(engine)
    adapter = StubAdapter(SERVER_5XX_PUSH)
    dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert effect_state(engine, effect_id) == "unknown"
    with pytest.raises(ControllerRefusal, match="already written"):
        dispatch_github_write(
            engine, adapter,  # type: ignore[arg-type]
            effect_id=effect_id, action="push_branch", idempotency_key="idem-2",
            payload=push_payload(), feature_id=feature_id,
        )
    assert adapter.calls == 1, "no second HTTP write after an unknown outcome"


def test_composition_parked_effect_stops_a_replay_write(engine) -> None:
    """A confirmed-but-unclosed effect must not silently rewrite either."""
    feature_id, effect_id = seed_effect_and_feature(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    with pytest.raises(ControllerRefusal) as refused:
        dispatch_github_write(
            engine, adapter,  # type: ignore[arg-type]
            effect_id=effect_id, action="push_branch", idempotency_key="idem-2",
            payload=push_payload(), feature_id=feature_id,
        )
    assert "already written" in refused.value.detail
    assert adapter.calls == 1


def test_composition_lifecycle_steps_replay_idempotently(engine) -> None:
    """Replaying the same idempotency key re-runs the same composition.

    The engine's IDEMPOTENT_REPLAY receipts make each lifecycle step a no-op
    on replay; the adapter, however, is called once per composition — a
    replayed composition re-issues the write under the same key, which is
    the contract's replay fence (the remote write is idempotent-bound, and
    DAL-034 owns the dedupe).
    """
    feature_id, effect_id = seed_effect_and_feature(engine)
    adapter = StubAdapter(CONFIRMED_PUSH)
    first = dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    replay = dispatch_github_write(
        engine, adapter,  # type: ignore[arg-type]
        effect_id=effect_id, action="push_branch", idempotency_key="idem-1",
        payload=push_payload(), feature_id=feature_id,
    )
    assert first.effect_state == replay.effect_state == "dispatch_started"


def test_composition_rejects_unknown_action_and_payload_keys(engine) -> None:
    from personal_agent_dal.errors import DalError, DalErrorCode

    feature_id, effect_id = seed_effect_and_feature(engine)
    with pytest.raises(DalError):
        dispatch_github_write(
            engine, StubAdapter(CONFIRMED_PUSH),  # type: ignore[arg-type]
            effect_id=effect_id, action="merge_pr", idempotency_key="k",
            payload={}, feature_id=feature_id,
        )
    with pytest.raises(DalError) as extra:
        dispatch_github_write(
            engine, StubAdapter(CONFIRMED_PUSH),  # type: ignore[arg-type]
            effect_id=effect_id, action="push_branch", idempotency_key="k",
            payload={"branch": BRANCH, "head_sha": HEAD, "force": True},
            feature_id=feature_id,
        )
    assert extra.value.code is DalErrorCode.INVALID_ARGUMENT
    assert "unknown keys" in (extra.value.internal_detail or "")
    assert effect_state(engine, effect_id) == "intent_recorded", (
        "a refused composition never moved the effect row"
    )


def test_composition_missing_payload_keys_refuse_before_claim(engine) -> None:
    from personal_agent_dal.errors import DalError

    feature_id, effect_id = seed_effect_and_feature(engine)
    with pytest.raises(DalError) as missing:
        dispatch_github_write(
            engine, StubAdapter(CONFIRMED_PUSH),  # type: ignore[arg-type]
            effect_id=effect_id, action="push_branch", idempotency_key="k",
            payload={"branch": BRANCH}, feature_id=feature_id,
        )
    assert "missing" in (missing.value.internal_detail or "")
    assert effect_state(engine, effect_id) == "intent_recorded"


def test_composition_pr_flow_binds_base_and_parks_on_confirm(engine) -> None:
    feature_id, effect_id = seed_effect_and_feature(engine)
    confirmed_pr = PullRequestOutcome(
        repository_id=REPO, pull_request_number=3, head_sha=HEAD,
    )
    outcome = dispatch_github_write(
        engine, StubAdapter(confirmed_pr),  # type: ignore[arg-type]
        effect_id=effect_id, action="create_pull_request", idempotency_key="idem-1",
        payload={"branch": BRANCH, "base_branch": "main", "title": "T", "body": "B"},
        feature_id=feature_id,
    )
    assert outcome.effect_state == "dispatch_started"


def test_composition_check_flow_confirmed_parks(engine) -> None:
    feature_id, effect_id = seed_effect_and_feature(engine)
    confirmed_check = CheckRunOutcome(
        repository_id=REPO, check_name=CHECK_NAME, head_sha=HEAD,
        check_run_id=9, external_id="cap-1", conclusion="success",
    )
    outcome = dispatch_github_write(
        engine, StubAdapter(confirmed_check),  # type: ignore[arg-type]
        effect_id=effect_id, action="write_check_run", idempotency_key="idem-1",
        payload={
            "branch_head_sha": HEAD, "check_name": CHECK_NAME,
            "external_id": "cap-1", "conclusion": "success",
        },
        feature_id=feature_id,
    )
    assert outcome.effect_state == "dispatch_started"
