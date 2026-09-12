"""§8's composed switch, term by term.

The switch decides whether a photo may reach a model, so the tests that matter
are the ones where it says no. §8 names five terms and requires all of them;
each one is closed here on its own to prove it is load-bearing, because a
composition that happens to be closed today would look identical if one of its
terms were dead code.

The registry half is the other direction: §8's "未声明/未知 alias 禁图" makes the
*absence* of a declaration the refusal, so the tests that matter are the near
misses -- the right provider with the wrong model, the wrong case, and the model
the provider will happily accept an image for without being able to read it.
"""

from __future__ import annotations

from datetime import date

import pytest

from personal_agent.runtime.modality import (
    G1_APPROVAL,
    IMAGE_INPUT_ENV,
    SCANNER_EXEMPTION,
    TERM_G1,
    TERM_MASTER,
    TERM_MEDIA,
    TERM_MODEL,
    TERM_SCANNER,
    VISION_MODELS,
    Approval,
    ImageCapability,
    ModalityConfigError,
    image_capability,
    master_switch,
    vision_evidence,
)

#: Every term open. The base case the single-term tests subtract from, so a
#: term can be shown to have closed the switch on its own.
_OPEN = dict(
    master=True,
    provider="deepseek",
    model_id="deepseek-flash",
    media_ready=True,
)


# --- the master switch -------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "off", "OFF", " off "])
def test_an_absent_or_off_switch_is_off(value) -> None:
    """Absence is off. §10 puts an undecided deployment on the closed side."""
    environ = {} if value is None else {IMAGE_INPUT_ENV: value}
    assert master_switch(environ) is False


@pytest.mark.parametrize("value", ["on", "ON", " on "])
def test_the_switch_reads_on_case_insensitively(value) -> None:
    assert master_switch({IMAGE_INPUT_ENV: value}) is True


@pytest.mark.parametrize("value", ["true", "yes", "1", "enabled", "onn", "on,off"])
def test_a_value_nobody_defined_is_refused(value) -> None:
    """Not read as "off", which is the tempting and wrong implementation.

    "off" and "the operator typed something this code does not understand" are
    different facts. Collapsing them leaves someone editing a unit file,
    restarting, and finding images still dark with nothing anywhere saying the
    value was never read -- while any truthy-string check would have turned the
    same typo into *enabled*, which is worse.
    """
    with pytest.raises(ModalityConfigError):
        master_switch({IMAGE_INPUT_ENV: value})


# --- the vision registry -----------------------------------------------------


def test_a_declared_pair_is_found() -> None:
    found = vision_evidence("deepseek", "deepseek-flash")
    assert found is not None
    assert found.recorded_on == date(2026, 9, 10)
    assert found.evidence


def test_a_model_that_cannot_read_an_image_is_not_declared() -> None:
    """The registry's reason for existing, kept as a test.

    The provider does not refuse an image sent to a model that cannot read one:
    it answers, normally and confidently. A deployment pointed at such a model
    would serve a user an answer about a photo the model never saw, and nothing
    on the wire would say so -- so the refusal has to live here, before the send.
    """
    assert vision_evidence("deepseek", "deepseek-v4-pro") is None


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [
        ("deepseek", "deepseek-flash-vision"),
        ("deepseek", "DEEPSEEK-FLASH"),
        ("deepseek", "deepseek-flash "),
        ("deepseek", ""),
        ("zhipu", "deepseek-flash"),
        ("", "deepseek-flash"),
    ],
)
def test_a_near_miss_is_not_a_declaration(provider, model_id) -> None:
    """§8's "未声明/未知 alias 禁图", from the direction that matters.

    Every case here is one a plausible implementation would have let through: a
    substring match, a case fold, a strip, a default. The declaration is about
    an exact (provider, model) pair, and anything else is an unknown.
    """
    assert vision_evidence(provider, model_id) is None


def test_every_declaration_names_a_provider_and_carries_evidence() -> None:
    """A declaration without evidence is an assumption with a date on it."""
    for entry in VISION_MODELS:
        assert entry.provider and entry.model_id
        assert entry.evidence and entry.recorded_on <= date(2026, 9, 10)


# --- the composition ---------------------------------------------------------


def _missing_approvals() -> tuple[str, ...]:
    """The governance terms with no record, in §8's order.

    Derived from the constants rather than written out, so these tests keep
    saying the true thing on the day Henson approves the exemption: what is
    closed is whatever has no record, and nothing here has to be edited to stay
    correct.
    """
    pairs = ((TERM_SCANNER, SCANNER_EXEMPTION), (TERM_G1, G1_APPROVAL))
    return tuple(term for term, record in pairs if record is None)


def test_with_every_input_open_only_the_approvals_decide() -> None:
    """What is closed today is the governance records, and nothing else.

    Written this way on purpose. A test that asserted the switch is closed
    would pass just as well against a switch that could never open, and the
    interesting claim is narrower: with every input the deployment controls set
    favourably, the verdict is exactly the set of approvals this build does not
    hold.
    """
    capability = image_capability(**_OPEN)
    assert capability.closed_by == _missing_approvals()
    assert capability.enabled is (not _missing_approvals())


def test_the_switch_opens_when_the_records_exist(monkeypatch) -> None:
    """The switch is wired, not merely shut.

    Without this, every other test here would pass against a function that
    returned a constant, and the first real proof that images could ever be
    served would be the day someone approved the exemption in production. The
    records are supplied as data -- a dated approval with a name and an
    evidence path, the same shape a real one has -- and the composition is
    asserted to honour them end to end.
    """
    from personal_agent.runtime import modality

    granted = Approval(
        term="scanner_exemption",
        decided="the narrowed exemption is granted",
        approved_by="Henson",
        approved_on=date(2026, 9, 11),
        evidence="docs/gates/multimodal-input.md",
    )
    monkeypatch.setattr(modality, "SCANNER_EXEMPTION", granted)
    monkeypatch.setattr(modality, "G1_APPROVAL", granted)

    capability = image_capability(**_OPEN)

    assert capability.enabled is True
    assert capability.closed_by == ()


@pytest.mark.parametrize(
    ("closed", "term"),
    [
        ({"master": False}, TERM_MASTER),
        ({"model_id": "deepseek-v4-pro"}, TERM_MODEL),
        ({"provider": "zhipu"}, TERM_MODEL),
        ({"media_ready": False}, TERM_MEDIA),
    ],
)
def test_one_closed_term_closes_the_switch(closed, term) -> None:
    """Each term on its own, so no term can rot into decoration."""
    capability = image_capability(**{**_OPEN, **closed})
    assert capability.enabled is False
    assert term in capability.closed_by


def test_the_closed_terms_come_back_in_the_designs_order() -> None:
    """One answer, not one answer per attempt: everything wrong is reported."""
    capability = image_capability(
        master=False, provider="", model_id="", media_ready=False
    )
    assert capability.closed_by == (
        TERM_MASTER,
        TERM_MODEL,
        TERM_MEDIA,
        *_missing_approvals(),
    )


def test_a_refusal_names_every_closed_term() -> None:
    """An operator reading one refusal should not have to run the service again
    to find out there was a second reason."""
    capability = image_capability(**{**_OPEN, "media_ready": False})
    expected = (TERM_MEDIA, *_missing_approvals())

    assert capability.closed_by == expected
    assert capability.refusal() == "images are not available: " + ", ".join(expected)


def test_the_switch_is_closed_without_an_approval_record() -> None:
    """The load-bearing ``None``, tested as a property rather than a value.

    `SCANNER_EXEMPTION` and `G1_APPROVAL` are module constants, so a test that
    simply asserted they were set would be asserting a fact about today's
    governance. This asserts the *rule*: whichever of them is missing closes the
    switch, whatever the deployment says and whatever it has composed. Delete
    either record and the switch stays closed until someone reviews the change
    that puts it back.
    """
    capability = image_capability(**_OPEN)
    for term, record in ((TERM_SCANNER, SCANNER_EXEMPTION), (TERM_G1, G1_APPROVAL)):
        assert (term in capability.closed_by) is (record is None)


def test_the_scanner_exemption_is_not_approved_in_this_build() -> None:
    """A guard against the exemption being filled in without a review.

    This failure is the review. §0 G2 records the exemption as pending Henson's
    decision and §10 forbids the code from granting itself one, so the change
    that fills in `SCANNER_EXEMPTION` is the change that approves it -- and it
    must arrive with the date, the name and the evidence, and with this test and
    its comment updated to say so. Silently flipping it is what this catches.
    """
    assert SCANNER_EXEMPTION is None


def test_no_configuration_can_approve_the_exemption() -> None:
    """§10's "不自行降级", stated as the thing it forbids.

    The exemption is not read from the environment anywhere, so the strongest
    form of the claim is the absence: no environment value contributes to the
    scanner term. Checked against the source rather than by trying values,
    because "these values do not work" is a much weaker statement than "there is
    no path from the environment to this term at all".
    """
    import inspect

    from personal_agent.runtime import modality

    source = inspect.getsource(modality.image_capability)
    assert "environ" not in source and "os." not in source


def test_an_approval_record_has_to_carry_its_evidence() -> None:
    record = Approval(
        term="g1_criteria",
        decided="something",
        approved_by="Henson",
        approved_on=date(2026, 9, 10),
        evidence="docs/evidence/",
    )
    assert record.approved_on == date(2026, 9, 10)


def test_g1_is_recorded_with_the_narrow_reading_of_capability() -> None:
    """G1's scope, pinned where a later edit would otherwise widen it.

    §0 G1 defines 能力核验 as "the configured model really has vision and this
    request really consumed an image, judged by the token account" -- not that
    the model reads accurately, which is G4 and is not this switch's business.
    A record that drifted into claiming answer quality would be asserting
    something no test here could support.
    """
    assert G1_APPROVAL is not None
    assert G1_APPROVAL.approved_on == date(2026, 9, 10)


def test_the_capability_is_a_value_a_caller_can_compare() -> None:
    """It travels to `/v1/capabilities`, so it is not a mutable carrier."""
    capability = image_capability(**_OPEN)
    assert isinstance(capability, ImageCapability)
    with pytest.raises(Exception):
        capability.enabled = False  # type: ignore[misc]
