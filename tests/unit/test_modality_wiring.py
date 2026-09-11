"""§8's switch, fed by the deployment rather than by a test's idea of one.

`tests/unit/test_modality.py` proves the composition is correct given its
inputs. This module answers the other question, the one a passing suite of pure
tests cannot: are the inputs the *real* ones? The switch is only worth having if
the model it checks evidence against is the model the gateway will send, and if
a deployment that composes no media really reports the media term closed.

So nothing here constructs the switch's inputs directly. The provider comes from
`provider_from_env`, the model from `resolved_model_id` via `glm_gateway_from_env`
itself, and the media surface from `media_config_from_env` run against a real
lock-fitted root. The two call sites are then asserted to agree -- which is the
only failure this file exists for: one of them quietly reading `MODEL_ID` on its
own would leave both correct in isolation and the pair wrong together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.api.composition import AgentServiceConfig, image_capability_from
from personal_agent.media.config import (
    ALLOWED_MIMES_ENV,
    CLAIM_TTL_ENV,
    IMAGE_PIXELS_PER_TOKEN_ENV,
    MAX_CONTENT_ENV,
    MAX_DIMENSION_ENV,
    RETENTION_TTL_ENV,
    ROOT_ENV,
    TARGET_TTL_ENV,
    media_config_from_env,
)
from personal_agent.media.locking import ensure_lock_files
from personal_agent.runtime.glm_gateway import glm_gateway_from_env
from personal_agent.runtime.modality import (
    IMAGE_INPUT_ENV,
    TERM_MEDIA,
    TERM_MODEL,
    vision_evidence,
)
from personal_agent.runtime.model_providers import provider_from_env, resolved_model_id

DEEPSEEK = "deepseek"
#: A real model a deployment could select, and not a vision model. The provider
#: answers an image question sent to it normally, so the registry is the only
#: thing that can refuse the selection -- which is what the term below tests.
BLIND = "deepseek-v4-pro"
DECLARED = "deepseek-flash"
FAKE_KEY = "not-a-real-credential"


@pytest.fixture
def env(monkeypatch):
    """A deployment's environment with every §8 input cleared.

    Cleared rather than assumed, because the ambient shell is exactly the kind
    of second source of truth this file is about: a developer running the suite
    with `MODEL_ID` exported would otherwise see different verdicts than CI.
    """
    for name in (
        "MODEL_ID",
        "MODEL_PROVIDER",
        "MODEL_API_BASE",
        IMAGE_INPUT_ENV,
        ROOT_ENV,
        MAX_CONTENT_ENV,
        MAX_DIMENSION_ENV,
        ALLOWED_MIMES_ENV,
        TARGET_TTL_ENV,
        CLAIM_TTL_ENV,
        RETENTION_TTL_ENV,
        IMAGE_PIXELS_PER_TOKEN_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _media(root: Path):
    """A composed media surface, through the real reader.

    Built from a lock-fitted root rather than by handing `MediaConfig` to the
    dataclass, so the term under test is fed the same object a boot would feed
    it -- an empty config object would satisfy `is not None` while proving
    nothing about the deployment that produced it.
    """
    for name in ("staging", "final", "quarantine"):
        (root / name).mkdir(parents=True, exist_ok=True)
    ensure_lock_files(root)
    environ = {
        ROOT_ENV: str(root),
        MAX_CONTENT_ENV: "31457280",
        MAX_DIMENSION_ENV: "12000",
        ALLOWED_MIMES_ENV: "image/jpeg",
        TARGET_TTL_ENV: "1800",
        CLAIM_TTL_ENV: "600",
        RETENTION_TTL_ENV: "604800",
        IMAGE_PIXELS_PER_TOKEN_ENV: "750",
        "PERSONAL_AGENT_MEDIA_MAX_TOTAL_BYTES": "1073741824",
        "PERSONAL_AGENT_MEDIA_MAX_UNBOUND_OBJECTS": "20",
        "PERSONAL_AGENT_MEDIA_MAX_CONCURRENT_UPLOADS": "2",
    }
    configured = media_config_from_env(environ)
    assert configured is not None
    return configured


def _config(tmp_path: Path, *, media: bool) -> AgentServiceConfig:
    """A config shaped like the CLI's, with only the media surface varying."""
    return AgentServiceConfig(
        database=tmp_path / "personal_agent.db",
        finance_mcp_url="http://127.0.0.1:8811/mcp",
        finance_control_url="http://127.0.0.1:8811",
        user_id="henson",
        media=_media(tmp_path / "media") if media else None,
    )


@pytest.mark.parametrize("model_id", [None, DECLARED, BLIND])
def test_the_switch_and_the_gateway_agree_about_which_model_is_sent(
    env, tmp_path: Path, model_id
) -> None:
    """The drift this wiring exists to prevent, asserted as agreement.

    §8 checks the deployment's model for recorded vision evidence, and the
    gateway sends it. If those two ever read `MODEL_ID` separately, an operator
    could switch models and get a switch that describes the previous one --
    green in every unit test, wrong in production. Both sides are therefore
    driven from the environment here and required to name the same model.
    """
    env.setenv("MODEL_PROVIDER", DEEPSEEK)
    env.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    if model_id is not None:
        env.setenv("MODEL_ID", model_id)

    provider = provider_from_env()
    gateway = glm_gateway_from_env()

    assert gateway.model_id == resolved_model_id(provider)

    # The switch's model input is the registry's answer about *that* string, so
    # an undeclared model and a closed term are the same statement -- and the
    # parametrisation above makes the claim non-vacuous in both directions:
    # the unset default and `deepseek-flash` are declared, `deepseek-v4-pro`
    # is not.
    capability = image_capability_from(_config(tmp_path, media=True))()
    declared = vision_evidence(provider.name, gateway.model_id)

    assert (TERM_MODEL in capability.closed_by) is (declared is None)


def test_a_model_that_cannot_read_an_image_closes_the_model_term(
    env, tmp_path: Path
) -> None:
    """The registry, reached through the deployment rather than through a table.

    `BLIND` is selectable, and the provider will answer an image question sent
    to it without complaint. A deployment that switches to it must lose the
    image surface, and the only thing positioned to notice is this wiring.
    """
    env.setenv("MODEL_PROVIDER", DEEPSEEK)
    env.setenv("MODEL_ID", BLIND)

    capability = image_capability_from(_config(tmp_path, media=True))()

    assert TERM_MODEL in capability.closed_by


def test_a_declared_model_does_not_close_the_model_term(env, tmp_path: Path) -> None:
    """The same path, one model over, so the term is shown to be discriminating."""
    env.setenv("MODEL_PROVIDER", DEEPSEEK)
    env.setenv("MODEL_ID", DECLARED)

    capability = image_capability_from(_config(tmp_path, media=True))()

    assert TERM_MODEL not in capability.closed_by


def test_a_deployment_without_media_reports_the_media_term(env, tmp_path: Path) -> None:
    """§5.4's "缺配置不启用图片", arriving through the composed config.

    `AgentServiceConfig.media is None` is how an unconfigured deployment looks
    from here, and the switch has to say so -- an operator who never set a media
    root should read that as the reason, not as an approval that is pending.
    """
    env.setenv("MODEL_PROVIDER", DEEPSEEK)
    env.setenv("MODEL_ID", DECLARED)

    capability = image_capability_from(_config(tmp_path, media=False))()

    assert TERM_MEDIA in capability.closed_by


def test_a_deployment_with_media_opens_the_media_term(env, tmp_path: Path) -> None:
    """And the other direction, so the term is not closed by construction."""
    env.setenv("MODEL_PROVIDER", DEEPSEEK)
    env.setenv("MODEL_ID", DECLARED)

    capability = image_capability_from(_config(tmp_path, media=True))()

    assert TERM_MEDIA not in capability.closed_by


def test_the_master_switch_is_read_on_every_call(env, tmp_path: Path) -> None:
    """§8's "热配置变化服务端再次校验", at the level the app consults it.

    The provider, the model and the media surface are fixed at boot, but the
    master switch is deliberately not: a deployment that turns images off must
    have them off on the next request, not after a restart. The capability is a
    callable for exactly this reason, and a closure that captured the switch's
    first value would satisfy every other test here.
    """
    env.setenv("MODEL_PROVIDER", DEEPSEEK)
    env.setenv("MODEL_ID", DECLARED)
    env.setenv(IMAGE_INPUT_ENV, "off")
    capability = image_capability_from(_config(tmp_path, media=True))

    assert capability().enabled is False

    env.setenv(IMAGE_INPUT_ENV, "on")
    # Not enabled -- the approvals are still pending -- but the master term has
    # to have moved, which is the only thing this test is about.
    assert "master_switch" not in capability().closed_by


# --- the boot requirement -----------------------------------------------------
#
# The value is a composition-time contract, so the only honest place to assert
# it is `cli.main`. Without this, a unit file that misspelled the switch would
# leave every offline test green and surface on the ECS as image turns that
# never turn on, with nothing in any log saying the value was never read.


class _ReachedTheServer(Exception):
    """Raised in place of serving, to prove the boot got that far."""


def _argv(tmp_path: Path) -> list[str]:
    return ["personal-agent-api", "--database", str(tmp_path / "personal_agent.db")]


def _boot(monkeypatch, tmp_path: Path, **env: str):
    """Drive `cli.main` up to the point of serving, with the sentinel in place."""
    import sys

    from personal_agent import cli

    async def reached(*_args, **_kwargs):
        raise _ReachedTheServer

    monkeypatch.setattr(cli, "_serve", reached)
    monkeypatch.setattr(sys, "argv", _argv(tmp_path))
    monkeypatch.setenv("PERSONAL_AGENT_WRITE_SWITCH_FILE", str(tmp_path / "switch.json"))
    monkeypatch.setenv("PERSONAL_AGENT_USER_ID", "henson")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return cli


def test_an_unreadable_master_switch_stops_the_boot(env, tmp_path: Path) -> None:
    """A typo is a startup failure, not a silent "off".

    Same rule the media values are held to one block earlier in the CLI, and for
    the same reason: the two are indistinguishable to whoever is reading the
    logs a week later, and only one of them is what they meant.
    """
    cli = _boot(monkeypatch=env, tmp_path=tmp_path, **{IMAGE_INPUT_ENV: "true"})

    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert IMAGE_INPUT_ENV in str(raised.value)


def test_an_absent_master_switch_is_off_and_the_service_still_starts(
    env, tmp_path: Path
) -> None:
    """§10's undecided deployment is closed, not broken.

    Without this case, the test above would pass just as well against a CLI that
    refused to boot whenever the variable was missing -- which would take the
    whole agent down, text turns included, over a feature that is meant to be
    off by default.
    """
    cli = _boot(monkeypatch=env, tmp_path=tmp_path)

    with pytest.raises(_ReachedTheServer):
        cli.main()
