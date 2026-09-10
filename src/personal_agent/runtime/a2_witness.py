"""The A2 witness: what actually left, checked against what was authorized.

Design §8 requires A2 -- "序列化后的实际 HTTP body" -- rather than the ADK
request object (A3) or LiteLlm's arguments (A1), because those two are
*intentions*. A2 is the only layer at which the claim "the photo the user
attached is the photo the provider received" is about bytes that were really
serialized for sending.

§8.1 records four facts that came out of the spike, not out of the design. Each
one is a way this file could look correct and not be:

1. **The hook must be a coroutine function.** ``httpx/_client.py`` does
   ``await hook(request)``. A plain ``def`` still runs its body and still
   records, then fails on ``await None``; the SDK reports that as
   ``APIConnectionError: Connection error`` -- indistinguishable from a network
   fault. :meth:`A2Witness.__call__` is ``async def``, and
   ``test_the_witness_is_a_coroutine_function`` fails if that ever changes.
2. **One outbound attempt per logical call.** Measured: without ``num_retries``
   a 500 produces three attempts; with the product's ``num_retries=0`` it
   produces one. More than one is a fail-closed condition, not a statistic.
3. **The witness never rewrites the request.** A witness that edited the body
   and then vouched for it would be endorsing its own edit.
4. **Passing a client moves URL authority from ``api_base`` to the client.**
   A decoy ``api_base`` is ignored once ``client=`` is supplied, so pinning the
   host cannot be left to ``api_base``. The witness checks scheme and host
   itself and refuses before the send.

This module is a re-derivation, not a port of ``scripts/spike_multimodal_a2_seam.py``.
Spike code is historical evidence (AGENTS.md §5.1); what it establishes is the
four constraints above, which are implemented here against the production call
path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from personal_agent.runtime.model_input import ImageInputPart


class A2Violation(RuntimeError):
    """The evidence does not support the claim that the image left intact.

    Always raised, never returned: §8.1 requires an outbound failure to block
    before the network and an evidence failure to block before the
    ``ModelProposal`` is returned, so a violated turn cannot reach the
    dispatcher.
    """


@dataclass(frozen=True)
class OutboundAttempt:
    """One serialized request, recorded immediately before transport."""

    method: str
    scheme: str
    host: str
    path: str
    body: bytes


@dataclass
class A2Witness:
    """Records each outbound attempt, and refuses any that is not pinned.

    The pin check is the reason this is a guard rather than a logger. §5.1
    requires a credential to travel only to the pinned endpoint; the credential
    rides in this request's headers, so refusing before the send is what makes
    that a mechanism instead of a claim.
    """

    pinned_host: str
    attempts: list[OutboundAttempt] = field(default_factory=list)
    #: Refusals are recorded *and* raised. If a transport layer ever wraps the
    #: raise into a connection error, the verifier still sees the refusal and
    #: reports the real reason instead of blaming the network.
    violations: list[str] = field(default_factory=list)

    async def __call__(self, request: Any) -> None:
        """The httpx request event hook. Must stay ``async`` -- see module docs."""
        scheme = request.url.scheme
        host = request.url.host
        if scheme != "https" or host != self.pinned_host:
            reason = (
                f"request targeted {scheme}://{host}, not the pinned "
                f"https://{self.pinned_host}"
            )
            self.violations.append(reason)
            raise A2Violation(reason)
        self.attempts.append(
            OutboundAttempt(
                method=request.method,
                scheme=scheme,
                host=host,
                path=request.url.path,
                # `request.content` materializes the body and raises if it was
                # never read. Reading it as an empty default would record a
                # body-less attempt as a passing evidence record.
                body=request.content,
            )
        )


def body_images(body: bytes) -> list[tuple[str, bytes]]:
    """Every inline image in an OpenAI-style chat body, as (mime, bytes).

    Returns an empty list for a body that is not the expected shape rather than
    raising: the caller's next step is to compare the count against the
    authorized set, and a body it cannot parse has a count of zero images. The
    alternative -- treating "unparseable" as "fine" -- is the failure this whole
    module exists to prevent.
    """
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(document, dict):
        return []
    found: list[tuple[str, bytes]] = []
    for message in document.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "image_url":
                continue
            url = (block.get("image_url") or {}).get("url")
            if not isinstance(url, str):
                continue
            header, separator, encoded = url.partition(",")
            if not separator or not header.startswith("data:") or ";base64" not in header:
                continue
            mime = header[len("data:") :].split(";", 1)[0]
            try:
                found.append((mime, base64.b64decode(encoded, validate=True)))
            except (binascii.Error, ValueError):
                continue
    return found


def _text_precedes_image(body: bytes) -> bool:
    """Whether text comes before the image in the message that carries one."""
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        return False
    for message in (document if isinstance(document, dict) else {}).get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        kinds = [block.get("type") for block in content if isinstance(block, dict)]
        if "image_url" in kinds:
            return "text" in kinds and kinds.index("text") < kinds.index("image_url")
    return False


def verify(
    witness: A2Witness,
    *,
    expected_images: tuple[ImageInputPart, ...],
    prompt_tokens: int | None,
) -> None:
    """The §8 A2 check. Fails closed; never repairs, truncates or drops.

    `prompt_tokens` is the numeric rule §8 demands rather than a presence check:
    when a response carries no ``usage`` field at all, ADK reports ``0`` and the
    two are indistinguishable at this layer (stage-two §3.1). The count is
    corroboration and carries no proof on its own -- it separates "the image was
    refused somewhere upstream" from "the request was sent", not "the model
    understood the image".
    """
    if witness.violations:
        raise A2Violation(witness.violations[0])
    if len(witness.attempts) != 1:
        raise A2Violation(
            f"expected exactly one outbound attempt, saw {len(witness.attempts)}"
        )
    attempt = witness.attempts[0]
    if attempt.method != "POST":
        raise A2Violation(f"unexpected outbound method {attempt.method}")

    found = body_images(attempt.body)
    if len(found) != len(expected_images):
        raise A2Violation(
            f"expected {len(expected_images)} image(s) in the outbound body, "
            f"saw {len(found)}"
        )
    for (mime, data), expected in zip(found, expected_images, strict=True):
        if mime != expected.mime_type:
            raise A2Violation(
                f"outbound image MIME {mime} is not the authorized "
                f"{expected.mime_type}"
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected.content_sha256:
            raise A2Violation(
                "outbound image bytes are not the authorized bytes "
                f"(sent {digest[:12]}…, authorized {expected.content_sha256[:12]}…)"
            )
    if expected_images and not _text_precedes_image(attempt.body):
        # §3.1's wire order is text then image, and §8 keeps that order at the
        # provider so the model reads the instruction before the picture.
        raise A2Violation("the outbound body places the image before its text")

    if prompt_tokens is None or prompt_tokens < 1:
        raise A2Violation(
            f"prompt token count is {prompt_tokens!r}; insufficient evidence, close"
        )
