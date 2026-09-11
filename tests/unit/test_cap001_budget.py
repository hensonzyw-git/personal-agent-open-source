"""CAP-001 slice E: the context budget.

Covers failure set F-E1..F-E6 (`docs/CAP-001失败集_v0.1.md` §5). The properties
under test are that the model never receives a truncated component, the same
estimate drives both trimming and reporting, and the shipped fallback is a
conservative UTF-8-byte upper bound. Real adapter accuracy remains CAP-001 H
evidence rather than something a fake estimator can prove.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from personal_agent.context.budget import (
    ComponentKind,
    ContextBudgeter,
    ContextComponent,
    HeuristicTokenEstimator,
    mark_covered,
)
from personal_agent.context.config import (
    CAP001_PROVISIONAL_VALUES,
    ContextConfig,
)
from personal_agent_core.errors import AppError, ErrorCode


def _config(**overrides) -> ContextConfig:
    values = dict(CAP001_PROVISIONAL_VALUES)
    values.update(overrides)
    return ContextConfig.from_mapping("ctx-test", values)


class _FixedEstimator:
    """One token per character, so a test can state sizes exactly."""

    version = "fixed-test-v1"

    def estimate(self, text: str) -> int:
        return len(text)


class _UnderestimatingEstimator:
    """Reports a tenth of the truth: the F-E1 failure, made concrete."""

    version = "underestimating-test-v1"

    def estimate(self, text: str) -> int:
        return len(text) // 10


def _mandatory(size: int = 10) -> list[ContextComponent]:
    return [
        ContextComponent(ComponentKind.SYSTEM_POLICY, "p" * size),
        ContextComponent(ComponentKind.USER_INPUT, "u" * size),
    ]


def _budgeter(estimator=None, **overrides) -> ContextBudgeter:
    return ContextBudgeter(_config(**overrides), estimator or _FixedEstimator())


# -- F-E2: everything counts, not just the user message --------------------


def test_all_components_count_towards_the_budget() -> None:
    budgeter = _budgeter(CONTEXT_ESTIMATE_SAFETY_MARGIN="0")
    components = [
        ContextComponent(ComponentKind.SYSTEM_POLICY, "p" * 10),
        ContextComponent(ComponentKind.USER_INPUT, "u" * 10),
        ContextComponent(ComponentKind.CHECKPOINT, "c" * 10),
        ContextComponent(ComponentKind.MEMORY, "m" * 10),
        ContextComponent(ComponentKind.TOOL_DECLARATION, "t" * 10),
        ContextComponent(ComponentKind.RAW_EVENT, "r" * 10),
    ]
    assert budgeter.total(components) == 60


def test_a_tool_schema_is_counted_as_the_json_it_is_sent_as() -> None:
    budgeter = _budgeter(CONTEXT_ESTIMATE_SAFETY_MARGIN="0")
    schema = {
        "name": "finance.log_expense",
        "parameters": {"type": "object", "properties": {"amount": {"type": "string"}}},
    }
    # Estimating the description alone is exactly the "only counted the user
    # message" mistake; the whole serialised schema is what travels.
    assert budgeter.estimate(schema) > len("finance.log_expense")


def test_the_safety_margin_is_applied_on_top_of_every_estimate() -> None:
    plain = _budgeter(CONTEXT_ESTIMATE_SAFETY_MARGIN="0")
    margined = _budgeter(CONTEXT_ESTIMATE_SAFETY_MARGIN="0.5")
    components = _mandatory(100)
    assert plain.total(components) == 200
    assert margined.total(components) == 300


def test_the_margin_rounds_up_never_down() -> None:
    budgeter = _budgeter(CONTEXT_ESTIMATE_SAFETY_MARGIN="0.15")
    # 10 tokens * 1.15 = 11.5, which must become 12, not 11.
    components = [
        ContextComponent(ComponentKind.SYSTEM_POLICY, "p" * 5),
        ContextComponent(ComponentKind.USER_INPUT, "u" * 5),
    ]
    assert budgeter.total(components) == 12


# -- F-E6: a conservative fallback estimator --------------------------------


def test_the_fallback_estimator_is_pessimistic_in_one_direction() -> None:
    estimator = HeuristicTokenEstimator()
    # One token per UTF-8 byte is deliberately wasteful but does not classify
    # Arabic, Indic scripts, combining marks or emoji as cheap ASCII.
    samples = ("记一笔午饭", "مرحبا بالعالم", "🧑‍💻", "कृपया", "ááá")
    for text in samples:
        assert estimator.estimate(text) == len(text.encode("utf-8"))
    assert estimator.estimate("") == 0
    assert estimator.version == "heuristic-utf8-bytes-v1"


def test_the_estimator_version_travels_with_the_outcome() -> None:
    outcome = _budgeter().fit(_mandatory())
    assert outcome.estimator_version == "fixed-test-v1"
    assert outcome.config_version.startswith("ctx-test.")


# -- F-E4: the trim order is fixed ------------------------------------------


def test_the_trim_order_is_memory_then_tools_then_covered_then_recent() -> None:
    # A hard limit that forces every tier to be sacrificed in turn.
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=100,
        CONTEXT_HARD_LIMIT_TOKENS=200,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    components = [
        *_mandatory(20),
        ContextComponent(ComponentKind.MEMORY, "m" * 100, weight=1),
        ContextComponent(ComponentKind.TOOL_DECLARATION, "t" * 100, weight=1),
        ContextComponent(
            ComponentKind.RAW_EVENT, "c" * 100, ordinal=1, covered_by_checkpoint=True
        ),
        ContextComponent(ComponentKind.RAW_EVENT, "r" * 100, ordinal=2),
    ]

    # 440 tokens against a 200 limit: memory, tools and the covered event go,
    # and the recent raw event survives because the order sacrifices relevance
    # before recency.
    outcome = budgeter.fit(components)
    kinds = [item.kind for item in outcome.components]
    assert ComponentKind.MEMORY not in kinds
    assert ComponentKind.TOOL_DECLARATION not in kinds
    remaining_events = [
        item for item in outcome.components if item.kind is ComponentKind.RAW_EVENT
    ]
    assert [item.ordinal for item in remaining_events] == [2]
    assert outcome.trimmed == (
        "dropped_memory",
        "narrowed_tool_and_router_candidates",
        "dropped_checkpoint_covered_events",
    )


def test_memory_is_dropped_least_relevant_first() -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=100,
        CONTEXT_HARD_LIMIT_TOKENS=150,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    components = [
        *_mandatory(20),
        ContextComponent(ComponentKind.MEMORY, "a" * 60, weight=9, label="high"),
        ContextComponent(ComponentKind.MEMORY, "b" * 60, weight=1, label="low"),
    ]
    outcome = budgeter.fit(components)
    kept = [item.label for item in outcome.components if item.label]
    assert kept == ["high"]


def test_raw_events_are_dropped_oldest_first() -> None:
    # 40 mandatory + 3 x 50 = 190 against a 100 limit, so two of the three
    # must go and the survivor has to be the newest.
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=50,
        CONTEXT_HARD_LIMIT_TOKENS=100,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    components = [
        *_mandatory(20),
        *(
            ContextComponent(ComponentKind.RAW_EVENT, "e" * 50, ordinal=index)
            for index in (1, 2, 3)
        ),
    ]
    outcome = budgeter.fit(components)
    survivors = [
        item.ordinal
        for item in outcome.components
        if item.kind is ComponentKind.RAW_EVENT
    ]
    assert survivors == [3]


def test_a_checkpoint_is_never_dropped_to_make_room() -> None:
    # Dropping the checkpoint would enlarge the input it exists to shrink.
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=100,
        CONTEXT_HARD_LIMIT_TOKENS=150,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    components = [
        *_mandatory(20),
        ContextComponent(ComponentKind.CHECKPOINT, "k" * 100),
        ContextComponent(ComponentKind.MEMORY, "m" * 100),
    ]
    outcome = budgeter.fit(components)
    assert any(
        item.kind is ComponentKind.CHECKPOINT for item in outcome.components
    )
    assert not any(
        item.kind is ComponentKind.MEMORY for item in outcome.components
    )


def test_a_checkpoint_that_cannot_fit_causes_refusal_not_history_loss() -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=50,
        CONTEXT_HARD_LIMIT_TOKENS=100,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    with pytest.raises(AppError) as excinfo:
        budgeter.fit(
            [
                *_mandatory(20),
                ContextComponent(ComponentKind.CHECKPOINT, "k" * 100),
            ]
        )
    assert excinfo.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


# -- F-E3 / F-E5: refuse rather than truncate -------------------------------


def test_mandatory_context_over_the_limit_refuses_instead_of_truncating() -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=10,
        CONTEXT_HARD_LIMIT_TOKENS=50,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    with pytest.raises(AppError) as excinfo:
        budgeter.fit(
            [
                ContextComponent(ComponentKind.SYSTEM_POLICY, "p" * 40),
                ContextComponent(ComponentKind.USER_INPUT, "u" * 40),
            ]
        )
    assert excinfo.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_the_exact_pending_state_is_never_dropped() -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=10,
        CONTEXT_HARD_LIMIT_TOKENS=50,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    with pytest.raises(AppError):
        budgeter.fit(
            [
                *_mandatory(5),
                # A parked duplicate decision's candidate set: dropping it
                # would let the answer be given without its facts.
                ContextComponent(ComponentKind.PENDING_STATE, "d" * 100),
            ]
        )


def test_an_essential_tool_survives_to_the_refusal() -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=10,
        CONTEXT_HARD_LIMIT_TOKENS=60,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    with pytest.raises(AppError) as excinfo:
        budgeter.fit(
            [
                *_mandatory(5),
                ContextComponent(
                    ComponentKind.TOOL_DECLARATION, "t" * 100, essential=True
                ),
            ]
        )
    assert excinfo.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_nothing_is_ever_returned_partially_rendered() -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=50,
        CONTEXT_HARD_LIMIT_TOKENS=120,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    original = [
        *_mandatory(20),
        ContextComponent(ComponentKind.MEMORY, "m" * 500),
        ContextComponent(ComponentKind.RAW_EVENT, "r" * 60, ordinal=1),
    ]
    outcome = budgeter.fit(original)
    # Every survivor is byte-identical to what went in: components leave whole
    # or not at all.
    by_kind = {item.kind: item.text for item in original}
    for item in outcome.components:
        assert item.text == by_kind[item.kind]


# -- F-E1: an estimator that guesses low still cannot exceed the limit ------


def test_an_underestimating_estimator_is_still_bounded_by_its_own_measure() -> None:
    # The budgeter can only ever act on the numbers it is given, so the defence
    # is twofold: the margin, and the fact that the *same* pessimistic estimate
    # is used for both the decision and the reported total. This pins the
    # second half -- a wrong estimator cannot make `fit` report a total it did
    # not enforce.
    budgeter = _budgeter(
        _UnderestimatingEstimator(),
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=10,
        CONTEXT_HARD_LIMIT_TOKENS=50,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    outcome = budgeter.fit(
        [
            *_mandatory(20),
            ContextComponent(ComponentKind.MEMORY, "m" * 1000),
        ]
    )
    assert outcome.estimated_input_tokens <= 50
    assert outcome.estimated_input_tokens == budgeter.total(outcome.components)


def test_the_reported_total_matches_the_components_returned() -> None:
    budgeter = _budgeter()
    outcome = budgeter.fit(_mandatory(30))
    assert outcome.estimated_input_tokens == budgeter.total(outcome.components)


# -- the soft limit is a Compactor trigger, not a boundary ------------------


def test_crossing_the_soft_limit_requests_compaction(caplog) -> None:
    budgeter = _budgeter(
        CONTEXT_PRODUCT_CEILING_TOKENS=1000,
        CONTEXT_SOFT_LIMIT_TOKENS=30,
        CONTEXT_HARD_LIMIT_TOKENS=500,
        CONTEXT_RESERVED_OUTPUT_TOKENS=1,
        CONTEXT_RESERVED_TOOL_TOKENS=1,
        CONTEXT_ESTIMATE_SAFETY_MARGIN="0",
    )
    outcome = budgeter.fit(_mandatory(40))
    assert outcome.compaction_requested is True
    # Nothing was dropped: the soft limit asks for a checkpoint, it does not
    # reduce the turn, and it is never a Session boundary (§6.3).
    assert outcome.trimmed == ()
    assert len(outcome.components) == 2


def test_staying_under_the_soft_limit_requests_nothing() -> None:
    outcome = _budgeter().fit(_mandatory(5))
    assert outcome.compaction_requested is False
    assert outcome.trimmed == ()


# -- construction invariants ------------------------------------------------


@pytest.mark.parametrize("count", [0, 2])
def test_a_turn_needs_exactly_one_user_input(count: int) -> None:
    components = [ContextComponent(ComponentKind.SYSTEM_POLICY, "p")]
    components.extend(
        ContextComponent(ComponentKind.USER_INPUT, "u") for _ in range(count)
    )
    with pytest.raises(AppError) as excinfo:
        _budgeter().fit(components)
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize("count", [0, 2])
def test_a_turn_needs_exactly_one_system_policy(count: int) -> None:
    components = [ContextComponent(ComponentKind.USER_INPUT, "u")]
    components.extend(
        ContextComponent(ComponentKind.SYSTEM_POLICY, "p") for _ in range(count)
    )
    with pytest.raises(AppError) as excinfo:
        _budgeter().fit(components)
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    "kind",
    [ComponentKind.SYSTEM_POLICY, ComponentKind.USER_INPUT],
)
def test_required_components_must_not_be_empty(kind: ComponentKind) -> None:
    components = _mandatory(1)
    components = [
        ContextComponent(item.kind, "   " if item.kind is kind else item.text)
        for item in components
    ]
    with pytest.raises(AppError) as excinfo:
        _budgeter().fit(components)
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


def test_mark_covered_flags_only_events_within_the_checkpoint_range() -> None:
    components = (
        ContextComponent(ComponentKind.RAW_EVENT, "a", ordinal=1),
        ContextComponent(ComponentKind.RAW_EVENT, "b", ordinal=5),
        ContextComponent(ComponentKind.MEMORY, "m", ordinal=1),
    )
    marked = mark_covered(components, through_sequence=3)
    assert [item.covered_by_checkpoint for item in marked] == [True, False, False]


def test_tiers_place_covered_history_ahead_of_the_recent_window() -> None:
    covered = ContextComponent(
        ComponentKind.RAW_EVENT, "a", ordinal=1, covered_by_checkpoint=True
    )
    recent = ContextComponent(ComponentKind.RAW_EVENT, "b", ordinal=2)
    assert covered.tier == 3
    assert recent.tier == 4
    assert ContextComponent(ComponentKind.SYSTEM_POLICY, "p").tier == 0
    assert ContextComponent(ComponentKind.CHECKPOINT, "k").tier == 0
    assert (
        ContextComponent(
            ComponentKind.TOOL_DECLARATION, "t", essential=True
        ).tier
        == 0
    )


def test_the_margin_is_exact_decimal_arithmetic() -> None:
    config = _config(CONTEXT_ESTIMATE_SAFETY_MARGIN="0.15")
    assert isinstance(config.estimate_safety_margin, Decimal)
    assert config.estimate_safety_margin == Decimal("0.15")


# -- §8: an image is part of the message, and it is paid for -----------------


def _image(tokens: int = 400) -> ContextComponent:
    """An image component: priced by its own rule, carrying no text."""
    return ContextComponent(
        ComponentKind.IMAGE_INPUT, "", label="image_input", ordinal=0, tokens=tokens
    )


def _priced(**overrides) -> ContextBudgeter:
    """A budgeter with the margin off, so the arithmetic in an assertion is exact."""
    return _budgeter(CONTEXT_ESTIMATE_SAFETY_MARGIN="0", **overrides)


def test_an_image_is_priced_by_its_bound_and_not_by_its_text() -> None:
    """§8's "尺寸/细节模式…保守上界", as arithmetic.

    The estimator has no way to price a photo: it counts characters, and the
    only string here is the empty label. Without `tokens` a photo would be
    charged nothing and every image turn would fit every budget on paper.
    """
    budgeter = _priced()
    assert budgeter.cost(_image(400)) == 400
    assert budgeter.total([*_mandatory(), _image(400)]) == 420


def test_a_component_with_its_own_cost_is_not_also_estimated_from_its_text() -> None:
    """Charged once. Adding the two would double an image's price."""
    assert _priced().cost(
        ContextComponent(ComponentKind.IMAGE_INPUT, "x" * 50, tokens=400)
    ) == 400


def test_a_component_may_not_cost_a_negative_number_of_tokens() -> None:
    with pytest.raises(AppError) as excinfo:
        _image(-1)
    assert excinfo.value.code is ErrorCode.INTERNAL_ERROR


def test_an_image_is_never_trimmed_to_make_a_turn_fit() -> None:
    """Dropping the image would ask the model about a photo it cannot see.

    Memory is what goes instead -- and the failure this rules out is silent:
    nothing downstream can tell that the question changed, so there would be no
    error to notice, only an answer about the wrong thing.
    """
    budgeter = _priced(
        CONTEXT_SOFT_LIMIT_TOKENS=100,
        CONTEXT_HARD_LIMIT_TOKENS=150,
    )
    outcome = budgeter.fit(
        [
            *_mandatory(10),
            ContextComponent(ComponentKind.MEMORY, "m" * 50),
            _image(100),
        ]
    )
    kinds = [item.kind for item in outcome.components]
    assert ComponentKind.IMAGE_INPUT in kinds
    assert ComponentKind.MEMORY not in kinds
    assert outcome.dropped_counts == {"memory": 1}


def test_an_image_that_cannot_fit_refuses_the_turn_instead_of_being_dropped() -> None:
    budgeter = _priced(
        CONTEXT_SOFT_LIMIT_TOKENS=100,
        CONTEXT_HARD_LIMIT_TOKENS=150,
    )
    with pytest.raises(AppError) as excinfo:
        budgeter.fit([*_mandatory(10), _image(1_000_000)])
    assert excinfo.value.code is ErrorCode.CONTEXT_BUDGET_EXCEEDED


def test_a_photo_with_no_words_is_a_full_question() -> None:
    """§8: 纯图片…不能被空文本校验…提前当空请求.

    A photo sent with no text is a complete request, and refusing it here would
    answer a legitimate turn with a budget error.
    """
    outcome = _priced().fit(
        [
            ContextComponent(ComponentKind.SYSTEM_POLICY, "p" * 10),
            ContextComponent(ComponentKind.USER_INPUT, ""),
            _image(400),
        ]
    )
    assert ComponentKind.IMAGE_INPUT in [item.kind for item in outcome.components]


def test_an_empty_message_without_a_photo_is_still_refused() -> None:
    """The relaxation is reached by the counted image, not by asking for it.

    There is no flag a caller can pass, so a turn that carries no image cannot
    arrive at the exemption at all.
    """
    with pytest.raises(AppError) as excinfo:
        _priced().fit(
            [
                ContextComponent(ComponentKind.SYSTEM_POLICY, "p" * 10),
                ContextComponent(ComponentKind.USER_INPUT, "   "),
                ContextComponent(ComponentKind.MEMORY, "m"),
            ]
        )
    assert "non-empty" in (excinfo.value.internal_detail or "")
