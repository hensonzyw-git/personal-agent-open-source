"""The one typed context configuration, validated at startup.

Cross-cutting design §7.1 and §19. Every number that bounds a model input lives
here, in one typed module, and nowhere else. The design is explicit that this
must *not* be spread across a prompt, the iOS client and a handful of
environment variables, so the baseline is a named, versioned value in code
rather than eight environment reads: an operator cannot half-configure it, and
the model and the client have no path to widen it at all.

Three properties are enforced structurally rather than by review:

- **No partial configuration.** `from_mapping` requires the complete key set. A
  mapping missing a key is refused instead of being merged onto a default,
  because a silently defaulted `hard_limit` is exactly the mistake that would
  send an over-budget input to the model.
- **The invariants hold or the service does not start.** `validate` re-checks
  the §7.1 inequality, and `require_within_model_limit` refuses a configuration
  larger than the limit the model adapter itself declares.
- **The version is derived from the content.** Editing any number changes
  `config_version`, which enters trace and evidence, so a run can never be
  attributed to a configuration it did not use.

The numbers below are **provisional**: design §7.1 freezes them only after the
CAP-001 eval, and they are deliberately not derived from the provider's maximum
window (cost, attention dilution and sensitive-data exposure all bind before the
window does). Freezing the post-eval values is a code change that mints a new
version and keeps the previous one loadable for rollback (§19.3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Mapping

from personal_agent_core.manifest import canonical_json


class ContextConfigError(RuntimeError):
    """The context configuration is incomplete, malformed or inconsistent.

    Raised at startup only. There is no runtime path that repairs a bad budget:
    a service that cannot bound its model input must not serve.
    """


#: The eight budget keys fixed by design §7.1, plus the Session and Timeline
#: paging values §19 assigns to the same CAP-001 configuration family. The
#: design names the first eight literally; the remaining three follow the same
#: `CONTEXT_` convention because §19.1 requires one typed module for the whole
#: family rather than a second configuration surface.
BUDGET_KEYS: Final[tuple[str, ...]] = (
    "CONTEXT_PRODUCT_CEILING_TOKENS",
    "CONTEXT_SOFT_LIMIT_TOKENS",
    "CONTEXT_HARD_LIMIT_TOKENS",
    "CONTEXT_RESERVED_OUTPUT_TOKENS",
    "CONTEXT_RESERVED_TOOL_TOKENS",
    "CONTEXT_ESTIMATE_SAFETY_MARGIN",
    "CONTEXT_MAX_SESSION_LINEAGE_DEPTH",
    "CONTEXT_FULL_REBUILD_AFTER_INCREMENTALS",
)

SESSION_KEYS: Final[tuple[str, ...]] = ("CONTEXT_SESSION_IDLE_MINUTES",)

TIMELINE_KEYS: Final[tuple[str, ...]] = (
    "CONTEXT_TIMELINE_PAGE_DEFAULT",
    "CONTEXT_TIMELINE_PAGE_MAX",
)

CONFIG_KEYS: Final[tuple[str, ...]] = BUDGET_KEYS + SESSION_KEYS + TIMELINE_KEYS

#: Every key except the margin is a positive whole number of tokens, minutes or
#: repetitions. The margin is a ratio and is carried as `Decimal`, never a
#: binary float: an inexact margin would quietly shrink the safety it exists to
#: provide.
_INTEGER_KEYS: Final[frozenset[str]] = frozenset(
    key for key in CONFIG_KEYS if key != "CONTEXT_ESTIMATE_SAFETY_MARGIN"
)

_MARGIN_MAX: Final[Decimal] = Decimal("1")


@dataclass(frozen=True)
class ContextConfig:
    """One complete, validated context configuration.

    Frozen because a request must never mutate the budget it is being measured
    against. A request may only *tighten* what it asks for (a smaller page size,
    for instance); no caller can widen a server limit (§19.4).
    """

    name: str
    product_ceiling_tokens: int
    soft_limit_tokens: int
    hard_limit_tokens: int
    reserved_output_tokens: int
    reserved_tool_tokens: int
    estimate_safety_margin: Decimal
    max_session_lineage_depth: int
    full_rebuild_after_incrementals: int
    session_idle_minutes: int
    timeline_page_default: int
    timeline_page_max: int

    def __post_init__(self) -> None:
        self.validate()

    # -- construction ----------------------------------------------------

    @classmethod
    def from_mapping(cls, name: str, values: Mapping[str, Any]) -> ContextConfig:
        """Build from the complete key set, or refuse.

        Refusing an unknown key matters as much as refusing a missing one: a
        typo that is ignored looks exactly like a value that was applied.
        """
        if not isinstance(name, str) or not name.strip():
            raise ContextConfigError("context configuration needs a name")
        supplied = set(values)
        missing = sorted(set(CONFIG_KEYS) - supplied)
        if missing:
            raise ContextConfigError(
                "context configuration is incomplete; missing "
                + ", ".join(missing)
            )
        unknown = sorted(supplied - set(CONFIG_KEYS))
        if unknown:
            raise ContextConfigError(
                "context configuration has unknown keys: " + ", ".join(unknown)
            )
        return cls(
            name=name,
            product_ceiling_tokens=_positive_int(
                values, "CONTEXT_PRODUCT_CEILING_TOKENS"
            ),
            soft_limit_tokens=_positive_int(values, "CONTEXT_SOFT_LIMIT_TOKENS"),
            hard_limit_tokens=_positive_int(values, "CONTEXT_HARD_LIMIT_TOKENS"),
            reserved_output_tokens=_positive_int(
                values, "CONTEXT_RESERVED_OUTPUT_TOKENS"
            ),
            reserved_tool_tokens=_positive_int(
                values, "CONTEXT_RESERVED_TOOL_TOKENS"
            ),
            estimate_safety_margin=_margin(values),
            max_session_lineage_depth=_positive_int(
                values, "CONTEXT_MAX_SESSION_LINEAGE_DEPTH"
            ),
            full_rebuild_after_incrementals=_positive_int(
                values, "CONTEXT_FULL_REBUILD_AFTER_INCREMENTALS"
            ),
            session_idle_minutes=_positive_int(
                values, "CONTEXT_SESSION_IDLE_MINUTES"
            ),
            timeline_page_default=_positive_int(
                values, "CONTEXT_TIMELINE_PAGE_DEFAULT"
            ),
            timeline_page_max=_positive_int(values, "CONTEXT_TIMELINE_PAGE_MAX"),
        )

    # -- validation ------------------------------------------------------

    def validate(self) -> None:
        """Re-check every §7.1 invariant. Called on construction."""
        if self.soft_limit_tokens >= self.hard_limit_tokens:
            raise ContextConfigError(
                "CONTEXT_SOFT_LIMIT_TOKENS must be below "
                "CONTEXT_HARD_LIMIT_TOKENS"
            )
        total = (
            self.hard_limit_tokens
            + self.reserved_output_tokens
            + self.reserved_tool_tokens
        )
        if total > self.product_ceiling_tokens:
            raise ContextConfigError(
                "hard limit plus reserved output and tool tokens must fit "
                "within CONTEXT_PRODUCT_CEILING_TOKENS"
            )
        if not Decimal("0") <= self.estimate_safety_margin < _MARGIN_MAX:
            raise ContextConfigError(
                "CONTEXT_ESTIMATE_SAFETY_MARGIN must be at least 0 and below 1"
            )
        if self.timeline_page_default > self.timeline_page_max:
            raise ContextConfigError(
                "CONTEXT_TIMELINE_PAGE_DEFAULT must not exceed "
                "CONTEXT_TIMELINE_PAGE_MAX"
            )

    def require_within_model_limit(self, model_limit: int | None) -> None:
        """Refuse a budget the model adapter cannot actually accept.

        `None` means the adapter declares no limit. That is not an implicit
        "unlimited": the product ceiling still binds on its own, and this
        function simply has nothing to compare against. An adapter that *does*
        declare a limit is authoritative over the configuration, never the
        other way round.
        """
        if model_limit is None:
            return
        if not isinstance(model_limit, int) or isinstance(model_limit, bool):
            raise ContextConfigError("declared model limit must be an integer")
        if model_limit <= 0:
            raise ContextConfigError("declared model limit must be positive")
        total = (
            self.hard_limit_tokens
            + self.reserved_output_tokens
            + self.reserved_tool_tokens
        )
        if total > model_limit:
            raise ContextConfigError(
                "context configuration exceeds the model adapter's declared "
                "context limit"
            )

    # -- identity --------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """The configuration as its frozen key set, for hashing and trace."""
        return {
            "CONTEXT_PRODUCT_CEILING_TOKENS": self.product_ceiling_tokens,
            "CONTEXT_SOFT_LIMIT_TOKENS": self.soft_limit_tokens,
            "CONTEXT_HARD_LIMIT_TOKENS": self.hard_limit_tokens,
            "CONTEXT_RESERVED_OUTPUT_TOKENS": self.reserved_output_tokens,
            "CONTEXT_RESERVED_TOOL_TOKENS": self.reserved_tool_tokens,
            "CONTEXT_ESTIMATE_SAFETY_MARGIN": str(self.estimate_safety_margin),
            "CONTEXT_MAX_SESSION_LINEAGE_DEPTH": self.max_session_lineage_depth,
            "CONTEXT_FULL_REBUILD_AFTER_INCREMENTALS": (
                self.full_rebuild_after_incrementals
            ),
            "CONTEXT_SESSION_IDLE_MINUTES": self.session_idle_minutes,
            "CONTEXT_TIMELINE_PAGE_DEFAULT": self.timeline_page_default,
            "CONTEXT_TIMELINE_PAGE_MAX": self.timeline_page_max,
        }

    @property
    def config_version(self) -> str:
        """A stable version derived from the name and every value.

        Content-derived on purpose: a hand-maintained version string can be
        forgotten, and evidence attributed to the wrong numbers is worse than no
        evidence. There is nothing secret, no user text and no external resource
        id in the hashed body (§19.5), so the digest is safe to log.
        """
        digest = hashlib.sha256(
            canonical_json({"name": self.name, "values": self.as_dict()}).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"{self.name}.{digest[:12]}"

    def page_limit(self, requested: int | None) -> int:
        """Resolve a client page size: default when absent, clamped down only.

        Never clamped *up*. A client asking for three events gets three; a
        client asking for a thousand gets the server maximum (§14).
        """
        if requested is None:
            return self.timeline_page_default
        if not isinstance(requested, int) or isinstance(requested, bool):
            raise ContextConfigError("page limit must be an integer")
        if requested < 1:
            raise ContextConfigError("page limit must be at least 1")
        return min(requested, self.timeline_page_max)


def _positive_int(values: Mapping[str, Any], key: str) -> int:
    value = values[key]
    # `bool` is an `int` subclass; `True` as a token count is a configuration
    # mistake, not a value of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContextConfigError(f"{key} must be an integer")
    if value <= 0:
        raise ContextConfigError(f"{key} must be positive")
    return value


def _margin(values: Mapping[str, Any]) -> Decimal:
    key = "CONTEXT_ESTIMATE_SAFETY_MARGIN"
    value = values[key]
    if isinstance(value, Decimal):
        margin = value
    elif isinstance(value, str):
        try:
            margin = Decimal(value)
        except InvalidOperation as exc:
            raise ContextConfigError(f"{key} is not a decimal") from exc
    else:
        # A float would make the margin inexact, and an int would silently
        # accept `1` as "100%", which the invariant forbids anyway.
        raise ContextConfigError(f"{key} must be a decimal string")
    if not margin.is_finite():
        raise ContextConfigError(f"{key} must be finite")
    return margin


#: The provisional CAP-001 baseline. Not the post-eval frozen configuration:
#: design §7.1 freezes these numbers only after the CAP-001 eval runs, and this
#: name says so out loud so no evidence can quietly claim the frozen version.
#: The ceiling is a product decision about cost, attention dilution and
#: sensitive-data exposure, not the provider's maximum window.
CAP001_PROVISIONAL_VALUES: Final[dict[str, Any]] = {
    "CONTEXT_PRODUCT_CEILING_TOKENS": 32000,
    "CONTEXT_SOFT_LIMIT_TOKENS": 16000,
    "CONTEXT_HARD_LIMIT_TOKENS": 24000,
    "CONTEXT_RESERVED_OUTPUT_TOKENS": 2048,
    "CONTEXT_RESERVED_TOOL_TOKENS": 4096,
    "CONTEXT_ESTIMATE_SAFETY_MARGIN": "0.15",
    "CONTEXT_MAX_SESSION_LINEAGE_DEPTH": 3,
    "CONTEXT_FULL_REBUILD_AFTER_INCREMENTALS": 3,
    "CONTEXT_SESSION_IDLE_MINUTES": 480,
    "CONTEXT_TIMELINE_PAGE_DEFAULT": 30,
    "CONTEXT_TIMELINE_PAGE_MAX": 100,
}


def default_context_config() -> ContextConfig:
    """The configuration the service composes with today."""
    return ContextConfig.from_mapping(
        "ctx-cap001-provisional-1", CAP001_PROVISIONAL_VALUES
    )
