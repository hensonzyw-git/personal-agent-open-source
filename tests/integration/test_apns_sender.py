"""The APNs sender's failure shapes. `DEV-040` / design 7.7 step 6.

A push provider is an adversarial boundary in the §5.1 sense: it is remote, it
answers with codes this code does not control, and the tempting bug is to treat
"it did not obviously fail" as delivery. So the failing cases are written first
and the passing one is almost an afterthought.

The counterparty here is a real `httpx2` mock transport rather than a hand-rolled
fake client, so the request that gets asserted is the request that would actually
go out.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from personal_agent.api.apns import (
    _HOSTS,
    ApnsConfig,
    ApnsConfigError,
    ApnsPushSender,
    ProviderToken,
    TOKEN_LIFETIME,
    build_payload,
    load_apns_config,
)
from personal_agent.api.notifications import PushNotification, PushSendError
from personal_agent.storage.engine import (
    create_all,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device
from personal_agent_core.crypto import KeyRing, generate_key


DEVICE_ID = "018f0000-0000-4000-8000-000000000001"
PUSH_TOKEN = "a1b2c3d4" * 8
NOTIFICATION = PushNotification(
    device_id=DEVICE_ID, review_id="rev_1", item_count=3
)


@pytest.fixture()
def private_key_pem(tmp_path: Path) -> Path:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "apns-auth.p8"
    path.write_bytes(pem)
    return path


def _env(key_path: Path, **overrides) -> dict[str, str]:
    env = {
        "PERSONAL_AGENT_APNS_ENVIRONMENT": "sandbox",
        "PERSONAL_AGENT_APNS_KEY_PATH": str(key_path),
        "PERSONAL_AGENT_APNS_KEY_ID": "KEYID12345",
        "PERSONAL_AGENT_APNS_TEAM_ID": "TEAMID6789",
        "PERSONAL_AGENT_APNS_TOPIC": "com.example.app",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing([generate_key("apns-test")], service="personal_agent")


@pytest.fixture()
def sessions(tmp_path: Path, keyring: KeyRing):
    engine = create_database_engine(tmp_path / "agent.sqlite")
    create_all(engine)
    factory = session_factory(engine)
    with factory() as session:
        session.add(
            Device(
                device_id=DEVICE_ID,
                display_name="iPhone",
                public_key="K",
                device_key_thumbprint="T",
                status="active",
                scopes="[]",
                allowed_tools_version="v1",
                created_at=__import__("datetime").datetime(
                    2026, 8, 3, tzinfo=__import__("datetime").timezone.utc
                ),
                encrypted_push_token=keyring.encrypt(
                    PUSH_TOKEN.encode("ascii"),
                    table="devices",
                    column="encrypted_push_token",
                    row_id=DEVICE_ID,
                ),
            )
        )
        session.commit()
    yield factory
    engine.dispose()


def _sender(config, sessions, keyring, handler, **kwargs) -> ApnsPushSender:
    client = httpx2.Client(
        base_url=config.base_url,
        transport=httpx2.MockTransport(handler),
    )
    return ApnsPushSender(
        config, session_factory=sessions, keyring=keyring, client=client, **kwargs
    )


@pytest.fixture()
def config(private_key_pem: Path) -> ApnsConfig:
    return load_apns_config(_env(private_key_pem))


# --- configuration: the host is pinned, never assembled -----------------------


def test_the_environment_selects_a_pinned_host_by_name(private_key_pem) -> None:
    sandbox = load_apns_config(_env(private_key_pem))
    production = load_apns_config(
        _env(private_key_pem, PERSONAL_AGENT_APNS_ENVIRONMENT="production")
    )
    assert sandbox.host == "api.sandbox.push.apple.com"
    assert production.host == "api.push.apple.com"
    assert set(_HOSTS.values()) == {sandbox.host, production.host}


def test_the_entitlements_development_spelling_is_refused(
    private_key_pem,
) -> None:
    """Apple calls the sandbox estate `development` in the iOS entitlement and
    `sandbox` in the host name; the ECS is provisioned with the host's word.

    Exactly one spelling is accepted, and it is the deployed one. Refusing the
    entitlement's word keeps the name-to-host mapping injective: there are
    exactly two reachable hosts and exactly one name for each, which is the
    property §5.1 is about.
    """
    with pytest.raises(ApnsConfigError, match="must be one of"):
        load_apns_config(
            _env(private_key_pem, PERSONAL_AGENT_APNS_ENVIRONMENT="development")
        )
    by_host = load_apns_config(
        _env(private_key_pem, PERSONAL_AGENT_APNS_ENVIRONMENT="sandbox")
    )
    assert by_host.host == "api.sandbox.push.apple.com"
    assert len(set(_HOSTS.values())) == 2


def test_an_unrecognised_environment_is_refused_not_defaulted(
    private_key_pem,
) -> None:
    """§5.1: a credential may only travel to a pinned endpoint.

    A default would silently send development tokens to the production estate,
    where they come back `BadDeviceToken` and read like a client bug for hours.
    """
    with pytest.raises(ApnsConfigError, match="must be one of"):
        load_apns_config(
            _env(private_key_pem, PERSONAL_AGENT_APNS_ENVIRONMENT="prod")
        )


def test_an_environment_that_is_a_url_cannot_redirect_the_credential(
    private_key_pem,
) -> None:
    with pytest.raises(ApnsConfigError):
        load_apns_config(
            _env(
                private_key_pem,
                PERSONAL_AGENT_APNS_ENVIRONMENT="https://evil.example.com",
            )
        )


@pytest.mark.parametrize(
    "missing",
    [
        "PERSONAL_AGENT_APNS_ENVIRONMENT",
        "PERSONAL_AGENT_APNS_KEY_PATH",
        "PERSONAL_AGENT_APNS_KEY_ID",
        "PERSONAL_AGENT_APNS_TEAM_ID",
        "PERSONAL_AGENT_APNS_TOPIC",
    ],
)
def test_every_input_is_required(private_key_pem, missing) -> None:
    with pytest.raises(ApnsConfigError, match=missing):
        load_apns_config(_env(private_key_pem, **{missing: None}))


def test_a_key_path_that_is_not_a_pem_is_refused(tmp_path) -> None:
    path = tmp_path / "not-a-key.p8"
    path.write_text("just some text", encoding="utf-8")
    with pytest.raises(ApnsConfigError, match="PEM private key"):
        load_apns_config(_env(path))


def test_a_missing_key_file_is_refused_at_composition(tmp_path) -> None:
    with pytest.raises(ApnsConfigError, match="could not be read"):
        load_apns_config(_env(tmp_path / "absent.p8"))


# --- the provider token -------------------------------------------------------


def test_the_provider_token_is_reused_then_refreshed(config) -> None:
    """Apple rejects tokens older than an hour AND clients that refresh more
    often than every 20 minutes. Both bounds matter, so the cache is not an
    optimisation."""
    clock = {"now": 1_000_000.0}
    token = ProviderToken(config, now=lambda: clock["now"])

    first = token.value()
    clock["now"] += 60
    assert token.value() == first, "refreshing every call trips the 20-minute bound"

    clock["now"] += TOKEN_LIFETIME.total_seconds()
    assert token.value() != first, "a token older than the window must be replaced"


def test_the_provider_token_is_es256_and_carries_the_key_id(config) -> None:
    import jwt as pyjwt

    raw = ProviderToken(config).value()
    header = pyjwt.get_unverified_header(raw)
    assert header["alg"] == "ES256"
    assert header["kid"] == config.key_id
    claims = pyjwt.decode(raw, options={"verify_signature": False})
    assert claims["iss"] == config.team_id
    assert "iat" in claims


# --- the payload carries a count, never an amount -----------------------------


def test_the_payload_holds_the_count_and_the_id_and_nothing_else() -> None:
    payload = build_payload(NOTIFICATION)
    assert payload["review_id"] == "rev_1"
    assert payload["aps"]["badge"] == 3
    flattened = json.dumps(payload, ensure_ascii=False)
    # Nothing that could be a ledger figure or a merchant name.
    for forbidden in ("CNY", "¥", "amount", "咖啡", "category"):
        assert forbidden not in flattened


# --- the send -----------------------------------------------------------------


def test_a_200_is_acceptance_and_carries_the_right_request(
    config, sessions, keyring
) -> None:
    seen: dict = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200)

    _sender(config, sessions, keyring, handler)(NOTIFICATION)

    assert seen["url"].endswith(f"/3/device/{PUSH_TOKEN}")
    assert seen["url"].startswith("https://api.sandbox.push.apple.com")
    assert seen["headers"]["apns-topic"] == config.topic
    assert seen["headers"]["apns-push-type"] == "alert"
    assert seen["headers"]["authorization"].startswith("bearer ")
    assert seen["headers"]["apns-collapse-id"] == "review:rev_1"
    assert seen["body"]["aps"]["badge"] == 3


@pytest.mark.parametrize(
    "status, reason",
    [
        (400, "BadDeviceToken"),
        (400, "DeviceTokenNotForTopic"),
        (403, "Forbidden"),
        (413, "PayloadTooLarge"),
    ],
)
def test_apples_permanent_reasons_stop_the_retry_loop(
    config, sessions, keyring, status, reason
) -> None:
    def handler(request):
        return httpx2.Response(status, json={"reason": reason})

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert excinfo.value.permanent is True
    # The reason is not echoed in the message: it is provider-controlled text
    # that reaches logs, so only the status code does. The closed known set is
    # still consulted to decide *permanence*, which is the property that matters.
    assert reason not in str(excinfo.value)
    assert str(status) in str(excinfo.value)


def test_a_410_is_permanent_whatever_the_body_says(
    config, sessions, keyring
) -> None:
    """410 is Apple saying the token is dead. Retrying it keeps a dead device in
    the queue while the card is already in the app."""

    def handler(request):
        return httpx2.Response(410, json={"reason": "Unregistered"})

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert excinfo.value.permanent is True


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_refusals_stay_retryable(
    config, sessions, keyring, status
) -> None:
    def handler(request):
        return httpx2.Response(status, json={"reason": "TooManyRequests"})

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert excinfo.value.permanent is False


def test_a_transport_failure_is_retryable_and_never_acceptance(
    config, sessions, keyring
) -> None:
    def handler(request):
        raise httpx2.ConnectError("no route")

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert excinfo.value.permanent is False


@pytest.mark.parametrize(
    "body",
    [b"not json at all", b"[]", b'{"no_reason": true}'],
    ids=["not-json", "json-array", "no-reason-key"],
)
def test_an_unparseable_error_body_still_fails_and_leaks_nothing(
    config, sessions, keyring, body
) -> None:
    """A provider must not be trusted to bound its own output.

    Whatever Apple returns, this fails closed with a stable string -- the raw
    body never reaches the message, because that string reaches logs.
    """

    def handler(request):
        return httpx2.Response(400, content=body)

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert "not json at all" not in str(excinfo.value)


def test_an_unrecognised_reason_string_never_reaches_the_message(
    config, sessions, keyring
) -> None:
    """§5.1: a provider must not bound its own behaviour.

    Apple's `reason` field is provider-controlled free text. Only a reason in
    the closed known set is trusted to branch on; an unrecognised value is
    collapsed to `unspecified` and never echoed, because that string reaches
    logs and a future provider change could put anything there.
    """

    def handler(request):
        return httpx2.Response(
            400, json={"reason": "ExploitAttempt\x00header-injection"}
        )

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert "ExploitAttempt" not in str(excinfo.value)
    assert "header-injection" not in str(excinfo.value)


# --- resolving the device's token ---------------------------------------------


def test_a_revoked_device_is_a_permanent_failure(config, sessions, keyring) -> None:
    import datetime

    with sessions() as session:
        device = session.get(Device, DEVICE_ID)
        device.status = "revoked"
        # The table's CHECK constraint requires the timestamp to match the
        # status, which is the schema refusing a half-revoked row.
        device.revoked_at = datetime.datetime(
            2026, 8, 3, tzinfo=datetime.timezone.utc
        )
        session.commit()

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("a revoked device must not reach Apple")

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert excinfo.value.permanent is True


def test_a_device_with_no_token_is_a_permanent_failure(
    config, sessions, keyring
) -> None:
    with sessions() as session:
        session.get(Device, DEVICE_ID).encrypted_push_token = None
        session.commit()

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("a tokenless device must not reach Apple")

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert excinfo.value.permanent is True


def test_an_unopenable_token_is_retryable_not_permanent(
    config, sessions
) -> None:
    """A key ring that cannot open the envelope may be a rotation mid-rollout.

    Discarding the card for that would be the wrong trade, so this is the one
    token-resolution failure that stays retryable.
    """
    stranger = KeyRing([generate_key("someone-else")], service="personal_agent")

    def handler(request):  # pragma: no cover - must never be reached
        raise AssertionError("an unopenable token must not reach Apple")

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, stranger, handler)(NOTIFICATION)
    assert excinfo.value.permanent is False


def test_the_push_token_never_appears_in_a_failure_message(
    config, sessions, keyring
) -> None:
    def handler(request):
        return httpx2.Response(400, json={"reason": "BadDeviceToken"})

    with pytest.raises(PushSendError) as excinfo:
        _sender(config, sessions, keyring, handler)(NOTIFICATION)
    assert PUSH_TOKEN not in str(excinfo.value)


# --- composition: absent is a choice, partial is a bug ------------------------


def test_no_apns_configuration_at_all_keeps_the_honest_refuser(
    sessions, keyring
) -> None:
    """A box with no Apple credentials is a legitimate deployment."""
    from personal_agent.api.apns import build_push_sender

    assert (
        build_push_sender(session_factory=sessions, keyring=keyring, env={})
        is None
    )


@pytest.mark.parametrize(
    "drop",
    [
        "PERSONAL_AGENT_APNS_ENVIRONMENT",
        "PERSONAL_AGENT_APNS_KEY_PATH",
        "PERSONAL_AGENT_APNS_KEY_ID",
        "PERSONAL_AGENT_APNS_TEAM_ID",
        "PERSONAL_AGENT_APNS_TOPIC",
    ],
)
def test_a_partial_configuration_refuses_instead_of_silently_not_pushing(
    sessions, keyring, private_key_pem, drop
) -> None:
    """The distinction the whole helper exists for.

    Falling back to "no push" on a typo would turn one wrong character into
    months of undelivered cards that look exactly like a deliberate choice.
    """
    from personal_agent.api.apns import ApnsConfigError, build_push_sender

    with pytest.raises(ApnsConfigError, match="partially configured"):
        build_push_sender(
            session_factory=sessions,
            keyring=keyring,
            env=_env(private_key_pem, **{drop: None}),
        )


def test_a_complete_configuration_builds_the_real_sender(
    sessions, keyring, private_key_pem
) -> None:
    from personal_agent.api.apns import build_push_sender

    sender = build_push_sender(
        session_factory=sessions, keyring=keyring, env=_env(private_key_pem)
    )
    try:
        assert isinstance(sender, ApnsPushSender)
    finally:
        sender.close()


def test_the_review_cli_wires_the_sender_rather_than_leaving_a_seam() -> None:
    """§7: a seam wired only in tests is not wiring.

    The review job's default is `UnavailablePushSender`, so if the CLI stopped
    passing `send=` every push would silently go back to refusing while every
    test above kept passing.
    """
    import ast

    source = (
        Path(__file__).resolve().parents[2]
        / "src/personal_agent/review_cli.py"
    ).read_text(encoding="utf-8")
    assert "build_push_sender(" in source
    tree = ast.parse(source)
    keywords = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_daily_review"
        for keyword in node.keywords
    }
    assert "send" in keywords
    # The Timeline card is the same class of seam: without these the run builds
    # cards and queues pushes but never seals the frozen `daily_review` event,
    # and the review would have no entry point once the page is gone.
    assert "keyring" in keywords
    assert "session_manager" in keywords
