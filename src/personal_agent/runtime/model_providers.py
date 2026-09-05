"""Provider registry for model gateways.

`DEV-027` pinned the model credential to one host: Zhipu's OpenAI-compatible
API path. Supporting a second provider without dropping that rule requires the
pin to become data instead of a hardcoded comparison. This module is that
data: every provider this codebase may send a credential to is declared here,
with its pinned HTTPS host, credential variable, and default model. Anything
not declared here fails closed before any network call, exactly as the
single-endpoint check did before.

The registry is deliberately not environment-configurable. A deployment can
choose *which* declared provider to use and *which* model to request; it
cannot teach this process a new endpoint at runtime. Adding a provider is a
code review, not a config edit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from personal_agent.runtime.model_gateway import ModelGatewayError

#: The environment variable selecting the active provider. The default keeps
#: every existing deployment on Zhipu without touching its environment.
PROVIDER_ENV = "MODEL_PROVIDER"

_ZHIPU_HOST = "open.bigmodel.cn"
_ZHIPU_PATH = "/api/paas/v4"
_DEEPSEEK_HOST = "api.deepseek.com"
_DEEPSEEK_PATH = ""


@dataclass(frozen=True)
class ModelProvider:
    """One declared provider and the credential rule that comes with it."""

    name: str
    #: The only HTTPS host this provider's credential may travel to.
    host: str
    #: The exact request path under ``host`` ("" meaning the root path).
    path: str
    #: The single credential variable that feeds this provider.
    credential_env: str
    #: The model used when the deployment does not name one.
    default_model: str


#: The declared set. Adding an entry is the code-review gate for sending a
#: credential to a new host.
PROVIDERS: dict[str, ModelProvider] = {
    item.name: item
    for item in (
        ModelProvider(
            name="zhipu",
            host=_ZHIPU_HOST,
            path=_ZHIPU_PATH,
            credential_env="ZAI_API_KEY",
            default_model="glm-5.3-flash",
        ),
        ModelProvider(
            name="deepseek",
            host=_DEEPSEEK_HOST,
            path=_DEEPSEEK_PATH,
            credential_env="DEEPSEEK_API_KEY",
            default_model="deepseek-v4-flash",
        ),
    )
}


def canonical_api_base(provider: ModelProvider) -> str:
    """The provider's pinned endpoint as a normalized HTTPS URL."""
    return f"https://{provider.host}{provider.path}/"


def provider_from_env(env: dict[str, str] | None = None) -> ModelProvider:
    """Resolve the active provider from ``MODEL_PROVIDER``.

    Unset means Zhipu, which is today's deployed behaviour. An unknown value
    fails closed: the registry, not the environment, decides where a
    credential may go.
    """
    source = os.environ if env is None else env
    name = (source.get(PROVIDER_ENV) or "zhipu").strip()
    provider = PROVIDERS.get(name.lower())
    if provider is None:
        raise ModelGatewayError(
            f"{PROVIDER_ENV} must be one of {sorted(PROVIDERS)}; got {name!r}"
        )
    return provider


def credential_from_env(
    provider: ModelProvider, env: dict[str, str] | None = None
) -> str:
    """Read exactly this provider's credential variable, and nothing else.

    A credential that does not name its provider risks being sent to the
    wrong host, so a Zhipu key under ``DEEPSEEK_API_KEY`` is as much a
    configuration error as a missing one. The strict prefix check turns that
    cross-wiring into a startup failure rather than a live leak attempt.
    """
    source = os.environ if env is None else env
    value = source.get(provider.credential_env, "").strip()
    if not value:
        raise ModelGatewayError(
            f"{provider.credential_env} is not set for provider "
            f"{provider.name!r}"
        )
    return value


def provider_for_api_base(api_base: str) -> str | None:
    """The declared provider whose pinned endpoint equals ``api_base``.

    ``generate_with_adk`` is a test seam whose fakes must keep today's
    signature (§5.2), so instead of threading the provider through it, the
    function resolves the provider from the already-validated endpoint.
    An unknown base returns ``None``; the caller decides what that means.
    """
    for name, provider in PROVIDERS.items():
        if api_base == canonical_api_base(provider):
            return name
    return None


def validated_api_base(value: str, provider: ModelProvider) -> str:
    """Validate ``value`` against the provider's pinned endpoint.

    Replaces the single-endpoint check: same fail-closed contract, one entry
    per declared provider. A URL that does not match the active provider's
    host, path, scheme, and absence of credentials/port/query is rejected
    before any network call, and the canonical endpoint is returned so the
    request record and the actual request cannot drift.
    """
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ModelGatewayError("GLM_OPENAI_BASE_URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != provider.host
        or (parsed.path.rstrip("/") if parsed.path else "") != provider.path
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelGatewayError(
            f"GLM_OPENAI_BASE_URL must be the pinned endpoint of provider "
            f"{provider.name!r}"
        )
    return canonical_api_base(provider)
