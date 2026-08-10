"""DAL-007: isolated service configuration, fail-closed at load.

The Development Agent Loop service must stay independent of the Finance
connectors and of every production credential. That independence is enforced
here, structurally, at the single point where the service assembles its
configuration -- not by review or by convention in the modules that consume it.

Four policy refusals are load-bearing and map onto the frozen
`DAL-T-CONFIG-ISOLATION-001` adversarial variants:

1. **Finance import** (`finance_import`): a config request that resolves to the
   Finance module namespace is refused. The DAL never reads the Finance
   connector's configuration surface.
2. **Insecure secret file** (`insecure_secret_file`): a secret materialised
   from a file whose OS mode is not owner-only is refused. A world- or
   group-readable secret file is a leak, not a config source.
3. **Production credential** (`production_credential`): a request naming a
   production or provider credential is refused. The DAL-007–013 slice holds no
   such credential, so any request for one is by definition out of policy.
4. **Unknown config** (`unknown_config`): a request for a config name outside
   the declared allowlist is refused. The service recognises exactly the names
   it declares; anything else is a refusal rather than a passthrough.

Every refusal is the same outward outcome: the service config stays
`not_loaded` and the operation receipt is `POLICY_DENIED`. The reason a load
was refused (which module, which secret, which path) is diagnostic and never
reaches the outward surface.

Reference: docs/开发Agent闭环开发拆解_v0.1.md §5 DAL-007;
docs/dal/DAL001-003_合同冻结包_v0.1.md §2.4 (TransitionCommand vocabulary).
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from personal_agent_dal.errors import DalError, DalErrorCode


#: The only config names the DAL service will ever load. Anything outside this
#: set is refused as `unknown_config`, so a typo or a smuggled name is a
#: refusal rather than a silent passthrough to the environment.
ALLOWED_CONFIG_NAMES: Final[frozenset[str]] = frozenset(
    {"DAL_DATABASE_URL", "DAL_AUDIT_KEY_REF"}
)

#: Logical module namespaces the DAL service is allowed to load config for.
#: These are logical identifiers, not Python import paths: they name which
#: service a config block belongs to. The Finance namespace is deliberately
#: absent -- see `_FORBIDDEN_MODULE_PREFIXES`.
ALLOWED_MODULE_PREFIXES: Final[tuple[str, ...]] = ("dal", "personal_agent_dal")

#: Module namespaces the DAL service must never load config for. The Finance
#: connector lives under `personal_agent.finance` / `personal_data_mcp`; a DAL
#: config request resolving there is the `finance_import` attack shape.
_FORBIDDEN_MODULE_PREFIXES: Final[tuple[str, ...]] = (
    "personal_agent.finance",
    "personal_data_mcp",
    "finance",
)

#: Credential names that are always refused. The DAL-007–013 slice holds no
#: production or provider credential, so any request for one of these is the
#: `production_credential` / secret-leak attack shape. Matching is on the
#: exact name and on obvious provider/secret prefixes, so a near-miss like
#: `FEISHU_APP_SECRET_V2` is refused too.
_FORBIDDEN_SECRET_NAMES: Final[frozenset[str]] = frozenset(
    {
        "PERSONAL_AGENT_DATA_KEY",
        "PERSONAL_AGENT_DATA_ACTIVE_KEY",
        "FEISHU_APP_SECRET",
        "FEISHU_APP_ID",
        "APNS_KEY",
        "APNS_PRIVATE_KEY",
    }
)
_FORBIDDEN_SECRET_PREFIXES: Final[tuple[str, ...]] = (
    "PERSONAL_AGENT_DATA",
    "FEISHU_",
    "APNS_",
    "GLM_",
    "OPENAI_",
    "ANTHROPIC_",
    "DEEPSEEK_",
    "CODEX_",
    "CLAUDE_",
)

#: A secret file must be owner-only. Anything wider is a leak, not a source.
_SECRET_FILE_ALLOWED_MODE: Final[int] = 0o600


class ConfigPolicy:
    """Decides whether a single declared config load is in policy.

    The policy is pure: it takes the *declared* attributes of a load (which
    module, which names, which secret file and its OS mode) and returns a
    decision. It performs no I/O of its own, so it can be driven by a fixture
    in a test exactly as it is driven by the real loader in production.
    """

    def check_module(self, module: str) -> None:
        """Refuse a config load that resolves outside the DAL namespace."""
        if not isinstance(module, str) or not module:
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail=f"empty module: {module!r}",
            )
        lowered = module.lower()
        for prefix in _FORBIDDEN_MODULE_PREFIXES:
            if lowered == prefix or lowered.startswith(prefix + "."):
                raise DalError(
                    DalErrorCode.CONFIG_POLICY_DENIED,
                    internal_detail=f"forbidden module namespace: {module!r}",
                )
        if not any(
            lowered == allowed or lowered.startswith(allowed + ".")
            for allowed in ALLOWED_MODULE_PREFIXES
        ):
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail=f"module outside DAL namespace: {module!r}",
            )

    def check_config_name(self, name: str) -> None:
        """Refuse a config name outside the declared allowlist."""
        if name not in ALLOWED_CONFIG_NAMES:
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail=f"undeclared config name: {name!r}",
            )

    def check_secret_name(self, name: str) -> None:
        """Refuse a request naming a production or provider credential."""
        if not isinstance(name, str):
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail=f"secret name not a string: {type(name).__name__}",
            )
        upper = name.upper()
        if upper in _FORBIDDEN_SECRET_NAMES or any(
            upper.startswith(prefix) for prefix in _FORBIDDEN_SECRET_PREFIXES
        ):
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail="request names a production/provider credential",
            )

    def check_secret_file_mode(self, path: str, mode: int | str | None) -> None:
        """Refuse a secret file whose OS mode is wider than owner-only.

        `mode` arrives either as an int (from `os.stat`) or as an octal string
        (from a fixture). Both are normalised; anything that is not exactly
        `0600` is refused.
        """
        resolved = self._normalise_mode(mode)
        if resolved != _SECRET_FILE_ALLOWED_MODE:
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail=(
                    f"secret file mode {oct(resolved)} is not "
                    f"{oct(_SECRET_FILE_ALLOWED_MODE)}"
                ),
            )

    @staticmethod
    def _normalise_mode(mode: int | str | None) -> int:
        if mode is None:
            # A secret file whose mode cannot be determined is not owner-only.
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail="secret file mode unavailable",
            )
        if isinstance(mode, bool):
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail="secret file mode is not a permission",
            )
        if isinstance(mode, int):
            # Strip file-type bits so a full st_mode compares against 0o600.
            return stat.S_IMODE(mode)
        if isinstance(mode, str):
            try:
                return int(mode, 8)
            except ValueError as exc:
                raise DalError(
                    DalErrorCode.CONFIG_POLICY_DENIED,
                    internal_detail=f"unparseable secret file mode: {mode!r}",
                ) from exc
        raise DalError(
            DalErrorCode.CONFIG_POLICY_DENIED,
            internal_detail=f"secret file mode of unexpected type: {type(mode).__name__}",
        )


@dataclass(frozen=True)
class ServiceConfig:
    """The assembled, in-policy service configuration.

    `state` is the DAL-007 entity state: it is `loaded` only when every
    declared value passed policy. A refused load never produces a
    `ServiceConfig`; the loader raises and the caller's state machine records
    `not_loaded` with a `POLICY_DENIED` receipt.
    """

    database_url: str
    audit_key_ref: str
    state: str = "loaded"


class ConfigLoader:
    """Loads the declared DAL service config from the environment, fail-closed.

    The loader is deliberately small: it resolves only the allowlisted names,
    applies the policy to every value's provenance, and refuses -- with a
    `POLICY_DENIED` receipt semantics -- on any deviation. It never reads a
    `.env` file, never falls back to a default, and never returns a partially
    populated config.
    """

    def __init__(self, policy: ConfigPolicy | None = None) -> None:
        self._policy = policy or ConfigPolicy()

    @property
    def policy(self) -> ConfigPolicy:
        return self._policy

    def load(
        self,
        *,
        module: str,
        environ: dict[str, str],
        secret_file: str | None = None,
        secret_file_mode: int | str | None = None,
    ) -> ServiceConfig:
        """Assemble the service config or raise `CONFIG_POLICY_DENIED`.

        The caller declares which logical module the config belongs to and
        supplies the environment mapping; the loader does not read
        `os.environ` itself, so a test can drive it with an exact fixture
        mapping. A secret-bearing file, when one is declared, is validated for
        its OS mode before any value is trusted.
        """
        self._policy.check_module(module)

        requested = {name: environ.get(name) for name in ALLOWED_CONFIG_NAMES}
        # Every declared name must be present and non-empty; a missing value is
        # an unavailability, not a policy refusal.
        missing = [name for name, value in requested.items() if not value]
        if missing:
            raise DalError(
                DalErrorCode.CONFIG_UNAVAILABLE,
                internal_detail=f"missing declared config: {sorted(missing)}",
            )

        if secret_file is not None:
            self._policy.check_secret_file_mode(secret_file, secret_file_mode)

        return ServiceConfig(
            database_url=requested["DAL_DATABASE_URL"] or "",
            audit_key_ref=requested["DAL_AUDIT_KEY_REF"] or "",
        )

    def validate_secret_file(self, path: Path) -> None:
        """Refuse a real on-disk secret file whose mode is not owner-only.

        This is the production counterpart of `check_secret_file_mode`: it
        stats the actual file. A missing file or a stat failure is a refusal.
        """
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise DalError(
                DalErrorCode.CONFIG_POLICY_DENIED,
                internal_detail=f"cannot stat secret file: {exc}",
            ) from exc
        self._policy.check_secret_file_mode(str(path), mode)
