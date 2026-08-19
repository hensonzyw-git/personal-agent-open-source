"""The real APNs sender. `DEV-040` / design 7.7 step 6.

Until now the only shipped `PushSender` was `UnavailablePushSender`, which
refuses honestly rather than recording a delivery that never happened. This is
the one that actually talks to Apple.

Three things it is careful about, each because the alternative is a specific
failure this project has already been bitten by:

**The host is pinned, not configured.** §5.1: a credential may only travel to a
pinned endpoint. `PERSONAL_AGENT_APNS_ENVIRONMENT` selects between two
*constants* here; it never supplies a URL. A tampered environment can pick the
wrong one of two Apple hosts and nothing else -- it cannot point the provider
JWT at an attacker.

**"Accepted" is never "seen".** A 200 from Apple means Apple took it. Nothing in
this module ever writes `reviewed`; only Henson acking the card does. That
separation is the whole reason the outbox exists, and it is why this returns
`None` on success rather than anything that could be mistaken for a receipt.

**The payload carries a count, never an amount.** `PushNotification` is
structurally incapable of holding a name or a sum, and the body built here adds
nothing beyond the count and the review id, because a lock-screen banner and
Apple's infrastructure are both places ledger data must not go.

The provider JWT is ES256 over `{iss: team, iat}` with `kid` in the header.
Apple rejects tokens older than an hour and rejects clients that mint them more
often than every 20 minutes, so it is cached and refreshed on a schedule between
those two bounds -- getting this wrong produces `TooManyProviderTokenUpdates`,
which looks like an auth bug and is not.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Final

import httpx2
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_agent.api.notifications import PushNotification, PushSendError
from personal_agent.storage.models import Device
from personal_agent_core.crypto import CryptoError, KeyRing


#: The only two hosts a provider token may be sent to. Selected by name from the
#: environment; never assembled from it.
#:
#: The sandbox estate has exactly one accepted spelling: the host's word,
#: `sandbox`. The iOS entitlement calls the same estate `development`
#: (`aps-environment: development`), but that is Apple's client-side vocabulary,
#: not this service's. The ECS is provisioned with `sandbox`
#: (`docs/evidence/DEV040_真机推送端到端_2026-08-04.md`), and accepting the
#: entitlement's spelling too would double the accepted input for no operational
#: reason -- the property §5.1 cares about is that exactly two hosts are
#: reachable, and one spelling per estate keeps the mapping injective.
_HOSTS: Final[dict[str, str]] = {
    "production": "api.push.apple.com",
    "sandbox": "api.sandbox.push.apple.com",
}

ENVIRONMENT_ENV: Final[str] = "PERSONAL_AGENT_APNS_ENVIRONMENT"
KEY_PATH_ENV: Final[str] = "PERSONAL_AGENT_APNS_KEY_PATH"
KEY_ID_ENV: Final[str] = "PERSONAL_AGENT_APNS_KEY_ID"
TEAM_ID_ENV: Final[str] = "PERSONAL_AGENT_APNS_TEAM_ID"
TOPIC_ENV: Final[str] = "PERSONAL_AGENT_APNS_TOPIC"

#: Apple refuses a provider token older than 60 minutes, and refuses a client
#: that refreshes more often than every 20. Anything strictly inside that window
#: is safe; 45 minutes leaves room for clock skew at both ends.
TOKEN_LIFETIME: Final[timedelta] = timedelta(minutes=45)

#: A daily review card is worth delivering for a day, not forever.
EXPIRATION: Final[timedelta] = timedelta(hours=12)

REQUEST_TIMEOUT: Final[float] = 10.0

#: Apple's reasons that will never succeed for this token or configuration. A
#: permanent failure stops the retry loop instead of burning five attempts.
_PERMANENT_REASONS: Final[frozenset[str]] = frozenset(
    {
        "BadDeviceToken",
        "DeviceTokenNotForTopic",
        "Unregistered",
        "BadTopic",
        "TopicDisallowed",
        "BadCertificate",
        "BadCertificateEnvironment",
        "Forbidden",
        "MissingDeviceToken",
        "BadExpirationDate",
        "PayloadTooLarge",
    }
)


class ApnsConfigError(RuntimeError):
    """The sender cannot be built. Raised at composition, never at send time."""


@dataclass(frozen=True)
class ApnsConfig:
    """Everything the sender needs, all of it validated at construction."""

    host: str
    key_id: str
    team_id: str
    topic: str
    private_key_pem: str

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"


def load_apns_config(env: dict[str, str] | None = None) -> ApnsConfig:
    """Read and validate the APNs inputs, or refuse to build a sender."""
    import os

    env = env if env is not None else dict(os.environ)

    def required(name: str) -> str:
        value = (env.get(name) or "").strip()
        if not value:
            raise ApnsConfigError(f"set {name}")
        return value

    environment = required(ENVIRONMENT_ENV)
    if environment not in _HOSTS:
        # Names, not URLs. An unrecognised name is a refusal rather than a
        # fallback, so a typo cannot silently push to the wrong Apple estate --
        # a development token sent to production is rejected as
        # `BadDeviceToken`, which reads like a client bug for hours.
        raise ApnsConfigError(
            f"{ENVIRONMENT_ENV} must be one of {sorted(_HOSTS)}, not {environment!r}"
        )

    key_path = Path(required(KEY_PATH_ENV))
    try:
        pem = key_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ApnsConfigError(
            f"{KEY_PATH_ENV} could not be read ({type(error).__name__})"
        ) from error
    if "PRIVATE KEY" not in pem:
        raise ApnsConfigError(f"{KEY_PATH_ENV} does not contain a PEM private key")

    return ApnsConfig(
        host=_HOSTS[environment],
        key_id=required(KEY_ID_ENV),
        team_id=required(TEAM_ID_ENV),
        topic=required(TOPIC_ENV),
        private_key_pem=pem,
    )


class ProviderToken:
    """The cached ES256 provider JWT, refreshed inside Apple's window."""

    def __init__(
        self, config: ApnsConfig, *, now: Callable[[], float] = time.time
    ) -> None:
        self._config = config
        self._now = now
        self._token: str | None = None
        self._minted_at: float = 0.0

    def value(self) -> str:
        moment = self._now()
        if self._token is None or moment - self._minted_at >= TOKEN_LIFETIME.total_seconds():
            self._token = jwt.encode(
                {"iss": self._config.team_id, "iat": int(moment)},
                self._config.private_key_pem,
                algorithm="ES256",
                headers={"kid": self._config.key_id},
            )
            self._minted_at = moment
        return self._token


def build_payload(notification: PushNotification) -> dict[str, Any]:
    """The push body. A count and an id; deliberately nothing else.

    Design 7.7 step 4 limits this to how many entries there are. No name, no
    amount, no category -- those would end up on a lock screen and inside
    Apple's infrastructure.

    The icon `badge` is deliberately **not** set: the number of items on one
    card is not the number of reviews still pending, and a stale badge that
    nothing clears is worse than no badge. The app shows 待你处理 itself and
    clears any icon badge when it comes to the foreground.
    """
    return {
        "aps": {
            "alert": {
                "title": "每日账目复核",
                "body": f"有 {notification.item_count} 笔待复核",
            },
            "sound": "default",
        },
        "review_id": notification.review_id,
    }


class ApnsPushSender:
    """A `PushSender` that hands notifications to Apple over HTTP/2."""

    def __init__(
        self,
        config: ApnsConfig,
        *,
        session_factory: Callable[[], Session],
        keyring: KeyRing,
        client: Any | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._sessions = session_factory
        self._keyring = keyring
        self._now = now
        self._token = ProviderToken(config, now=now)
        # HTTP/2 is not optional: APNs speaks nothing else.
        self._client = client or httpx2.Client(
            base_url=config.base_url, http2=True, timeout=REQUEST_TIMEOUT
        )

    def close(self) -> None:
        self._client.close()

    # --- the token this notification is for ---------------------------------

    def _device_token(self, device_id: str) -> str:
        """Read and open the device's push token, or refuse permanently.

        `PushNotification` carries no token on purpose -- a provider is told a
        count and an id, and the token is resolved here from the row. A device
        that has since been revoked or cleared its token is a permanent failure:
        retrying cannot make a missing token appear.
        """
        with self._sessions() as session:
            # An ORM `select`, not `session.get`: §5.2. The outbox worker's
            # session may have read this row earlier in its retry loop, and the
            # identity map would hand back that stale object even after another
            # session committed a revocation. A permanent-failure decision must
            # be made from the database's current state, never from a cached
            # snapshot. The ORM form (rather than a raw string) keeps the
            # `EncryptedEnvelope` TypeDecorator's decoding on the result column.
            row = session.execute(
                select(Device.status, Device.encrypted_push_token).where(
                    Device.device_id == device_id
                )
            ).one_or_none()
            if row is None or row[0] != "active":
                raise PushSendError(
                    "device is unknown or revoked", permanent=True
                )
            envelope = row[1]
            if envelope is None:
                raise PushSendError("device has no push token", permanent=True)
            try:
                return self._keyring.decrypt(
                    envelope,
                    table="devices",
                    column="encrypted_push_token",
                    row_id=device_id,
                ).decode("ascii")
            except (CryptoError, UnicodeDecodeError) as error:
                # Not permanent: a key ring that cannot open this envelope today
                # may be a rotation that has not finished rolling out, and
                # discarding the card for that would be the wrong trade.
                raise PushSendError(
                    f"push token could not be opened ({type(error).__name__})"
                ) from error

    # --- the send ------------------------------------------------------------

    def __call__(self, notification: PushNotification) -> None:
        device_token = self._device_token(notification.device_id)
        try:
            response = self._client.post(
                f"/3/device/{device_token}",
                json=build_payload(notification),
                headers={
                    "authorization": f"bearer {self._token.value()}",
                    "apns-topic": self._config.topic,
                    "apns-push-type": "alert",
                    "apns-priority": "5",
                    "apns-expiration": str(
                        int(self._now() + EXPIRATION.total_seconds())
                    ),
                    # The review id is the natural collapse key: a requeued or
                    # retried delivery of the same card should replace a still-
                    # pending banner rather than stack a second one. Apple keeps
                    # one pending notification per (device, topic, collapse-id),
                    # so without this a retry that raced the first would land as
                    # two "有 N 笔待复核" banners on one lock screen.
                    "apns-collapse-id": f"review:{notification.review_id}",
                },
            )
        except Exception as error:  # noqa: BLE001 - any transport failure retries
            raise PushSendError(
                f"APNs was unreachable ({type(error).__name__})"
            ) from error

        if response.status_code == 200:
            return

        reason = _reason_of(response)
        if response.status_code == 410 or reason in _PERMANENT_REASONS:
            # 410 is Apple telling us the token is dead. Retrying it forever
            # would keep a dead device in the queue; the card is still in the app.
            raise PushSendError(
                f"APNs rejected permanently: {response.status_code}",
                permanent=True,
            )
        raise PushSendError(f"APNs refused: {response.status_code}")


#: The full set of reason strings this code ever trusts enough to branch on.
#: Anything Apple returns outside this set is treated as `unspecified` and never
#: reaches a message, because §5.1 says a provider must not bound its own output:
#: the reason field is provider-controlled free text that reaches logs.
_KNOWN_REASONS: Final[frozenset[str]] = _PERMANENT_REASONS | frozenset(
    {"Unspecified", "PayloadEmpty", "TooManyRequests"}
)


def _reason_of(response: Any) -> str:
    """Apple's machine-readable reason, or a stable placeholder.

    Never the raw body, and never an unrecognised reason string: both are
    provider output, and this value reaches logs and the decision of whether a
    failure is permanent. Only a reason in the closed known set is passed
    through; everything else collapses to `unspecified`.
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body is Apple's business
        return "unparseable"
    if not isinstance(body, dict):
        return "unparseable"
    reason = body.get("reason")
    if isinstance(reason, str) and reason in _KNOWN_REASONS:
        return reason
    return "unspecified"


def apns_sender_from_env(
    *,
    session_factory: Callable[[], Session],
    keyring: KeyRing,
    env: dict[str, str] | None = None,
) -> ApnsPushSender:
    """Compose the sender, or raise so the service fails at boot rather than
    at the first review card."""
    return ApnsPushSender(
        load_apns_config(env),
        session_factory=session_factory,
        keyring=keyring,
    )


def build_push_sender(
    *,
    session_factory: Callable[[], Session],
    keyring: KeyRing,
    env: dict[str, str] | None = None,
) -> ApnsPushSender | None:
    """The real sender when APNs is configured, `None` when it deliberately is not.

    The distinction that matters is between *absent* and *partial*:

    - **no APNs variable set at all** is a legitimate deployment (a box with no
      Apple credentials), so this returns `None` and the caller keeps
      `UnavailablePushSender`, which refuses honestly instead of recording a
      delivery that never happened;
    - **some set and some missing** is a misconfiguration, and it raises. Falling
      back to "no push" there would turn a typo into months of silently
      undelivered cards that look exactly like a deliberate choice.
    """
    import os

    env = env if env is not None else dict(os.environ)
    names = (ENVIRONMENT_ENV, KEY_PATH_ENV, KEY_ID_ENV, TEAM_ID_ENV, TOPIC_ENV)
    present = [name for name in names if (env.get(name) or "").strip()]
    if not present:
        return None
    if len(present) != len(names):
        missing = sorted(set(names) - set(present))
        raise ApnsConfigError(
            f"APNs is partially configured; missing {missing}. Set all of them "
            "or none: a partial configuration is a typo, not a decision to go "
            "without push."
        )
    return apns_sender_from_env(
        session_factory=session_factory, keyring=keyring, env=env
    )
