"""The context budget: what fits in one model turn, and what is dropped first.

Cross-cutting design §7. Two rules make this module worth having:

- **Nothing is ever truncated by length.** A JSON tool schema cut in half, an
  amount missing its last digit or a receipt id shortened by two characters are
  all worse than a refusal, because they look like data. Components are dropped
  whole, in a fixed order, or the turn is refused with
  `CONTEXT_BUDGET_EXCEEDED`.
- **The order is fixed, not opportunistic** (§7.3). Relevance goes first and
  exactness goes last: memory, then router/tool breadth, then history already
  covered by a checkpoint, then the recent raw window. What is left -- the
  system policy, an existing Checkpoint, the user's current message, the exact
  state of a waiting operation and the minimal tool contract -- is never
  dropped. If *that* does not fit, the honest answer is that the turn cannot be
  built.

The estimate is deliberately pessimistic. An estimator that guesses low is the
failure mode that matters (F-E1): it produces an input the provider rejects
after the user has already waited, so the safety margin from configuration is
applied on top of every estimate and the comparison is made against the margined
total.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Iterable, Protocol

from personal_agent.context.config import ContextConfig
from personal_agent_core.errors import AppError, ErrorCode
from personal_agent_core.manifest import canonical_json


class ComponentKind(StrEnum):
    """What a piece of the model input is, which decides when it is dropped."""

    SYSTEM_POLICY = "system_policy"
    CAPABILITY_SUMMARY = "capability_summary"
    PREFERENCES = "preferences"
    CHECKPOINT = "checkpoint"
    RAW_EVENT = "raw_event"
    PENDING_STATE = "pending_state"
    MEMORY = "memory"
    CLARIFICATION_CONTEXT = "clarification_context"
    USER_INPUT = "user_input"
    IMAGE_INPUT = "image_input"
    TOOL_DECLARATION = "tool_declaration"


#: Never dropped, in any order, for any budget. Losing the policy would unbind
#: the model from its instructions; losing the current input would answer a
#: different question; losing the exact pending state would let a parked
#: clarification or duplicate decision be answered without its facts. A
#: Checkpoint is optional to build but, when present, it is the only retained
#: representation of history already removed from the raw window.
#:
#: `IMAGE_INPUT` is here for the reason that is easy to miss: an image is not a
#: droppable attachment beside the message, it *is* part of the message. Drop it
#: and the model is asked about a photo it cannot see, which is the silent
#: answer-nothing failure §5.1 forbids -- and it is unrecoverable at this layer,
#: because nothing downstream can tell that the question changed. §8 puts the
#: image "计入 mandatory input" and this is what that means mechanically.
MANDATORY_KINDS: Final[frozenset[ComponentKind]] = frozenset(
    {
        ComponentKind.SYSTEM_POLICY,
        ComponentKind.CHECKPOINT,
        ComponentKind.CLARIFICATION_CONTEXT,
        ComponentKind.USER_INPUT,
        ComponentKind.IMAGE_INPUT,
        ComponentKind.PENDING_STATE,
    }
)

#: The §7.3 trim order. A lower tier is sacrificed first. `CHECKPOINT` is
#: absent on purpose: it is what *replaces* raw history, so dropping it would
#: enlarge the input it exists to shrink.
TRIM_TIERS: Final[dict[ComponentKind, int]] = {
    ComponentKind.MEMORY: 1,
    ComponentKind.PREFERENCES: 2,
    ComponentKind.CAPABILITY_SUMMARY: 2,
    ComponentKind.TOOL_DECLARATION: 2,
    ComponentKind.RAW_EVENT: 3,
}

#: Reason codes recorded in trace when something is dropped. Enumerations only.
TRIM_REASONS: Final[dict[int, str]] = {
    1: "dropped_memory",
    2: "narrowed_tool_and_router_candidates",
    3: "dropped_checkpoint_covered_events",
    4: "reduced_recent_raw_window",
}


class TokenEstimator(Protocol):
    """Counts input tokens for a string. Versioned, because trace records it."""

    version: str

    def estimate(self, text: str) -> int: ...


class HeuristicTokenEstimator:
    """The fallback used when the model adapter exposes no tokenizer (§7.2).

    Deliberately pessimistic, and only in one direction: one token per UTF-8
    byte. Byte-fallback tokenizers cannot emit more tokens than the bytes they
    encode, so Arabic, Indic scripts, combining marks and emoji stay bounded
    instead of being misclassified as cheap "narrow" characters. The current
    adapter tokenizer remains preferred because this fallback intentionally
    wastes budget.
    """

    version: Final[str] = "heuristic-utf8-bytes-v1"

    def estimate(self, text: str) -> int:
        return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ContextComponent:
    """One droppable piece of the model input, already rendered to text.

    `text` is what will actually be sent, so the estimate and the payload can
    never disagree. Rendering (including the untrusted-data framing history and
    checkpoints must carry) belongs to the Context Builder, not here.
    """

    kind: ComponentKind
    text: str
    #: Raw events use `timeline_sequence`; the oldest is trimmed first.
    ordinal: int = 0
    #: Memory relevance and tool essentiality. Lower is dropped first.
    weight: int = 0
    #: Set on a raw event whose content is already summarised by an active
    #: checkpoint. Those are dropped a whole tier before uncovered history.
    covered_by_checkpoint: bool = False
    #: A tool the current intent cannot be completed without. Design §7.3 step
    #: 5 keeps the *minimal* tool contract even at the hard limit.
    essential: bool = False
    #: For trace only; never user text.
    label: str = ""
    #: A cost the component's own rule already computed, in tokens. Image input
    #: is the only such component: its cost is a function of the client's
    #: declared pixels and the deployment's coefficient (§8's "尺寸/细节模式…保守
    #: 上界"), and there is no text whose length could stand in for it -- the
    #: estimator would price a photo at the byte count of whatever string
    #: described it. `None` means "the cost is of `text`", which is every other
    #: component in this module.
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.tokens is not None and self.tokens < 0:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                internal_detail="a component may not cost a negative number of tokens",
            )

    @property
    def mandatory(self) -> bool:
        return self.kind in MANDATORY_KINDS or (
            self.kind is ComponentKind.TOOL_DECLARATION and self.essential
        )

    @property
    def tier(self) -> int:
        """Which trim step removes this, or 0 for never."""
        if self.mandatory:
            return 0
        if (
            self.kind is ComponentKind.RAW_EVENT
            and not self.covered_by_checkpoint
        ):
            # Step 4: the recent raw window, reduced only after everything the
            # checkpoint already covers has gone.
            return 4
        return TRIM_TIERS.get(self.kind, 4)


@dataclass(frozen=True)
class BudgetOutcome:
    """What the builder may send, and what had to go to make it fit."""

    components: tuple[ContextComponent, ...]
    estimated_input_tokens: int
    soft_limit: int
    hard_limit: int
    estimator_version: str
    config_version: str
    #: Enumerated reason codes, in the order they were applied.
    trimmed: tuple[str, ...] = ()
    #: True when the input crossed the soft limit, which is the Compactor's
    #: trigger (§7.3). It is a signal, never a Session boundary (§6.3).
    compaction_requested: bool = False
    dropped_counts: dict[str, int] = field(default_factory=dict)


class ContextBudgeter:
    """Measures a candidate context and reduces it to something that fits."""

    def __init__(
        self, config: ContextConfig, estimator: TokenEstimator | None = None
    ) -> None:
        self._config = config
        self._estimator = estimator or HeuristicTokenEstimator()

    @property
    def estimator_version(self) -> str:
        return self._estimator.version

    def estimate(self, value: str | dict[str, Any] | list[Any]) -> int:
        """Estimate one payload. Structured values are canonically serialised.

        A tool schema is sent as JSON, so it is counted as JSON: estimating its
        prose description alone is exactly the "only counted the user message"
        mistake of F-E2.
        """
        text = value if isinstance(value, str) else canonical_json(value)
        return self._estimator.estimate(text)

    def component(
        self, kind: ComponentKind, value: str | dict[str, Any] | list[Any], **kwargs
    ) -> ContextComponent:
        text = value if isinstance(value, str) else canonical_json(value)
        return ContextComponent(kind=kind, text=text, **kwargs)

    def total(self, components: Iterable[ContextComponent]) -> int:
        """The margined total. Every caller compares against this, not the raw sum."""
        raw = sum(self.cost(item) for item in components)
        margin = Decimal(1) + self._config.estimate_safety_margin
        return int((Decimal(raw) * margin).to_integral_value(rounding="ROUND_CEILING"))

    def cost(self, component: ContextComponent) -> int:
        """One component's raw cost, before the margin.

        Both directions are deliberate. A component carrying its own `tokens`
        is *not* also estimated from its text: the two would be added, and an
        image would then be charged twice -- once for the pixels and once for
        the stand-in string. A component without one is estimated exactly as
        before, so every pre-media input costs what it always cost.
        """
        if component.tokens is not None:
            return component.tokens
        return self._estimator.estimate(component.text)

    def fit(
        self, components: Iterable[ContextComponent]
    ) -> BudgetOutcome:
        """Reduce a candidate context to the hard limit, or refuse.

        Never returns a partially-rendered component: the reduction is by whole
        components, in tier order, oldest and least relevant first.
        """
        remaining = list(components)
        self._require_required_components(remaining)

        trimmed: list[str] = []
        dropped: dict[str, int] = {}
        total = self.total(remaining)
        compaction_requested = total > self._config.soft_limit_tokens

        for tier in (1, 2, 3, 4):
            if total <= self._config.hard_limit_tokens:
                break
            candidates = [item for item in remaining if item.tier == tier]
            if not candidates:
                continue
            # Least relevant first, then oldest first. Both are stable, so two
            # runs over the same input trim the same things.
            candidates.sort(key=lambda item: (item.weight, item.ordinal))
            for candidate in candidates:
                if total <= self._config.hard_limit_tokens:
                    break
                remaining.remove(candidate)
                dropped[candidate.kind.value] = (
                    dropped.get(candidate.kind.value, 0) + 1
                )
                total = self.total(remaining)
            if TRIM_REASONS[tier] not in trimmed and dropped:
                trimmed.append(TRIM_REASONS[tier])

        if total > self._config.hard_limit_tokens:
            # Step 5. What is left is the system policy, any existing
            # Checkpoint, the current message, the exact pending state and the
            # minimal tool contract. There is nothing safe left to remove, and
            # truncating any of it would send the model something that looks
            # like data but is not.
            raise AppError(
                ErrorCode.CONTEXT_BUDGET_EXCEEDED,
                internal_detail=(
                    f"mandatory context estimates {total} tokens against a hard "
                    f"limit of {self._config.hard_limit_tokens}"
                ),
            )

        return BudgetOutcome(
            components=tuple(remaining),
            estimated_input_tokens=total,
            soft_limit=self._config.soft_limit_tokens,
            hard_limit=self._config.hard_limit_tokens,
            estimator_version=self.estimator_version,
            config_version=self._config.config_version,
            trimmed=tuple(trimmed),
            compaction_requested=compaction_requested,
            dropped_counts=dropped,
        )

    def _require_required_components(
        self, components: list[ContextComponent]
    ) -> None:
        """A turn needs one non-empty policy and one non-empty current message.

        Checked before any measurement: an input missing the policy would run
        unbound, while one missing the user's message would "fit" beautifully
        and answer nothing.
        """
        required = (
            (ComponentKind.SYSTEM_POLICY, "system policy"),
            (ComponentKind.USER_INPUT, "user input"),
        )
        # §8: "纯图片与带图澄清不能被空文本校验…提前当空请求". A photo sent with no
        # words is a full question, and refusing it here would answer with a
        # budget error for a turn that is not over budget. The evidence is the
        # counted image component itself rather than a flag a caller passes, so
        # the relaxation cannot be reached by a turn that carries no image.
        imaged = any(
            item.kind is ComponentKind.IMAGE_INPUT for item in components
        )
        for kind, label in required:
            matches = [item for item in components if item.kind is kind]
            if len(matches) != 1:
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    internal_detail=(
                        f"a model turn needs exactly one {label}, got "
                        f"{len(matches)}"
                    ),
                )
            if not isinstance(matches[0].text, str) or not matches[0].text.strip():
                if kind is ComponentKind.USER_INPUT and imaged:
                    continue
                raise AppError(
                    ErrorCode.INTERNAL_ERROR,
                    internal_detail=f"a model turn needs a non-empty {label}",
                )


def mark_covered(
    components: Iterable[ContextComponent], *, through_sequence: int
) -> tuple[ContextComponent, ...]:
    """Flag raw events an active checkpoint already summarises.

    Kept separate from the checkpoint store so the budget can be reasoned about
    without one: a Timeline with no checkpoint simply has nothing covered, and
    the recent raw window is then the only history there is.
    """
    return tuple(
        replace(item, covered_by_checkpoint=True)
        if item.kind is ComponentKind.RAW_EVENT
        and item.ordinal <= through_sequence
        else item
        for item in components
    )
