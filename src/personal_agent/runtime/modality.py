"""§8's composed switch, and the registry of which models may be sent an image.

The design states the switch as one sentence -- "总开关 AND (pinned provider
身份, 实际 model_id) 能力证据 AND 媒体/备份/删除就绪 AND 扫描批准 AND G1 判据
通过" -- and then adds the two rules that give it teeth: the client's capability
and the server's entry are computed from the same source, and "未声明/未知 alias
禁图". This module is that source.

Two kinds of term live here, and the distinction is the point:

**Facts about this process.** Whether the media surface is composed is
observable here, and it is read, not asserted.

**Decisions made by people.** A scanner exemption is not a runtime setting. If
it were an environment variable, then whoever can edit a unit file could waive
a safety control, and the waiver would leave no review trail -- which is what
§0 G2 asks Henson to grant and §10 forbids the code from granting itself
("不自行降级"). So the two governance terms are :class:`Approval` records:
dated, attributable, and changed only by a code review. G1 was passed on
2026-09-10; the scanner exemption has not been granted, and its record is
``None``. That ``None`` is load-bearing and is the reason images are off today.
When Henson grants it, the record is filled in with its date and evidence --
in a reviewed change, which is exactly what the decision is.

§8 lists "媒体/备份/删除就绪" as a single term. Only part of it is visible from
inside this process: a composed media surface is a fact about this build. The
rest -- §7's backup producer holding the deletion manifest off-machine, the
cleanup path being scheduled -- belongs to a different identity on a different
schedule, and §7 already puts it there. Rather than fake a check for something
this process cannot observe, the operator's master switch carries that part,
and the docstring says so rather than letting it read as a silent ``True``.

Nothing here is consulted on a text-only turn. §8: "文本模型路径不受图片关闭
影响" -- a deployment with images off is still a deployment, and a turn that
names no image never reaches this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

#: §8's master switch. Absence is off: an operator who has not said they want
#: images has not decided, and §10 puts "保持关闭" on the undecided side.
IMAGE_INPUT_ENV = "PERSONAL_AGENT_MEDIA_IMAGE_INPUT"

_ON = "on"
_OFF = "off"


class ModalityConfigError(RuntimeError):
    """A modality setting could not be read. Raised at startup, never at a
    request -- the same contract as `MediaConfigError`."""


@dataclass(frozen=True)
class Approval:
    """A governance decision this build depends on, and who made it.

    These are code rather than configuration on purpose. An exemption from a
    frozen security control is a decision that has to carry a date, a name and
    a pointer to its evidence; a boolean in a systemd unit carries none of the
    three and can be flipped without anyone reviewing the reason.
    """

    term: str
    decided: str
    approved_by: str
    approved_on: date
    evidence: str


#: §0 G1. Passed 2026-09-10. What it settles is stated narrowly in the design
#: and must stay narrow here: 能力核验 means the configured model really has
#: vision and this request really consumed an image, judged by the token
#: account -- not that the model reads accurately, which is G4's picture-quality
#: eval and is not this switch's business.
#:
#: The A2 half of G1 is not this record's to assert: it is enforced by
#: `a2_witness` on every image turn, so a build in which A2 stopped holding
#: would refuse at the gateway rather than keep this record's promise for it.
G1_APPROVAL: Approval | None = Approval(
    term="g1_criteria",
    decided=(
        "FR-PHOTO-05's self-proof boundary is narrowed to verifiable final "
        "outbound integrity; 能力核验 is judged by the token account, not by "
        "answer accuracy"
    ),
    approved_by="Henson",
    approved_on=date(2026, 9, 10),
    evidence="docs/evidence/多模态输入spike阶段一_2026-09-10.md (+阶段二/三)",
)

#: Henson accepted the scanner exemption for single-user chat images on
#: 2026-09-14. This is risk acceptance, not a successful malware scan.
#: Header/size/resource limits and outbound image integrity remain mandatory.
SCANNER_EXEMPTION: Approval | None = Approval(
    term="scanner_exemption",
    decided="Temporarily exempt independent malware scanning for single-user chat images; retain all other image controls",
    approved_by="Henson",
    approved_on=date(2026, 9, 14),
    evidence="docs/gates/multimodal-input.md",
)


@dataclass(frozen=True)
class VisionEvidence:
    """One `(provider, model)` pair this build has evidence can see an image.

    The registry exists because the fact had nowhere to live. The
    2026-09-10 vision probe recorded the gap in its own words: `ModelProvider`
    carried host, path, credential and default model, and "「哪些模型接受图片」
    这一事实在代码里无处表达". The provider does not answer it either -- send an
    image to a model that cannot read one and it returns a normal 200 with a
    confident answer -- so a deployment that switched `MODEL_ID` to such a model
    would serve a user an answer about a photo the model never saw, and nothing
    on the wire would say so. §8's answer is that this registry is the refusal:
    FR-PHOTO-04 rejects before sending, and FR-PHOTO-05's delivery self-check is
    only the lower bound behind it.

    A pair is listed here only with evidence, and the model id is the *exact*
    wire id the deployment must set in `MODEL_ID`. §8's "未声明/未知 alias 禁图"
    is what makes an unlisted id a refusal rather than an assumption.
    """

    provider: str
    model_id: str
    evidence: str
    recorded_on: date


#: §8's "能力证据". Adding an entry is a claim about a provider's behaviour and
#: needs the same kind of evidence the existing two have: a live probe against
#: the pinned endpoint with the image's consumption shown in the token account.
#:
#: `deepseek-v4-pro` is deliberately absent, and its absence is the registry
#: working: it is not a vision model, so pointing `MODEL_ID` at it would return
#: a confident answer about an image it cannot read. The 2026-09-10 editorial
#: correction is why this is stated as a capability the model lacks rather than
#: as a defect in the provider -- the model behaves correctly, and it is the
#: *selection* that has to be caught, before the request leaves.
VISION_MODELS: tuple[VisionEvidence, ...] = (
    VisionEvidence(
        provider="deepseek",
        model_id="deepseek-flash",
        evidence="docs/evidence/DeepSeek_vision能力探针_2026-09-10.md",
        recorded_on=date(2026, 9, 10),
    ),
    VisionEvidence(
        provider="deepseek",
        model_id="deepseek-v4-flash-vision-exp",
        evidence="docs/evidence/DeepSeek_vision能力探针_2026-09-10.md",
        recorded_on=date(2026, 9, 10),
    ),
)


def vision_evidence(provider: str, model_id: str) -> VisionEvidence | None:
    """The declaration covering this provider and model id, or `None`.

    Both parts have to match. A declaration is about a *pair*: the same wire id
    served by a different host is a different model as far as this system can
    check, and §8 makes the provider identity half of the term.
    """
    for entry in VISION_MODELS:
        if entry.provider == provider and entry.model_id == model_id:
            return entry
    return None


@dataclass(frozen=True)
class ImageCapability:
    """§8's verdict, with every term that closed it named.

    All the closed terms, not the first one: an operator reading a refusal
    needs to know everything that is missing, and a client asking what it may
    send needs one answer rather than one answer per attempt. The order is
    §8's own, so the same deployment always reports the same list.
    """

    enabled: bool
    closed_by: tuple[str, ...]

    def refusal(self) -> str:
        """The internal detail for a refusal. Never shown to a client."""
        return "images are not available: " + ", ".join(self.closed_by)


#: The term names, in §8's order. They are stable identifiers: they appear in
#: refusal details and in operator-facing logs, so renaming one is a change to
#: something a runbook may quote.
TERM_MASTER = "master_switch"
TERM_MODEL = "model_vision_evidence"
TERM_MEDIA = "media_readiness"
TERM_SCANNER = "scanner_exemption"
TERM_G1 = "g1_criteria"


def master_switch(environ: Mapping[str, str] | None = None) -> bool:
    """Read §8's 总开关, refusing a value nobody defined.

    Three outcomes, and the third is why this is not `bool(os.environ.get(...))`:
    unset or `off` is a deployment that has not turned images on, `on` is one
    that has, and anything else is a typo an operator has to see at boot. A
    typo read as "off" would leave someone editing a unit file and restarting,
    with the feature still dark and nothing to say why.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(IMAGE_INPUT_ENV) or "").strip().lower()
    if not raw or raw == _OFF:
        return False
    if raw == _ON:
        return True
    raise ModalityConfigError(
        f"{IMAGE_INPUT_ENV} must be {_ON!r} or {_OFF!r}; got {raw!r}"
    )


def image_capability(
    *,
    master: bool,
    provider: str,
    model_id: str,
    media_ready: bool,
) -> ImageCapability:
    """Compose §8's five terms into the one verdict everything else reads.

    Every caller that needs to know whether an image may be used -- the chat
    entry, the anchor that binds one to a message, `/v1/capabilities`, and the
    §6 read that will hand bytes to the gateway -- calls this, so the client's
    answer and the server's answer cannot drift apart.

    `provider` and `model_id` are the *resolved* pair: the provider the
    deployment selected and the model id the gateway will actually send.
    Passing an alias, or a model the deployment did not end up using, would
    make the evidence term answer a question nobody asked.
    """
    closed: list[str] = []
    if not master:
        closed.append(TERM_MASTER)
    if vision_evidence(provider, model_id) is None:
        closed.append(TERM_MODEL)
    if not media_ready:
        closed.append(TERM_MEDIA)
    if SCANNER_EXEMPTION is None:
        closed.append(TERM_SCANNER)
    if G1_APPROVAL is None:
        closed.append(TERM_G1)
    return ImageCapability(enabled=not closed, closed_by=tuple(closed))


__all__ = [
    "Approval",
    "G1_APPROVAL",
    "IMAGE_INPUT_ENV",
    "ImageCapability",
    "ModalityConfigError",
    "SCANNER_EXEMPTION",
    "TERM_G1",
    "TERM_MASTER",
    "TERM_MEDIA",
    "TERM_MODEL",
    "TERM_SCANNER",
    "VISION_MODELS",
    "VisionEvidence",
    "image_capability",
    "master_switch",
    "vision_evidence",
]
