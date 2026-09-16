"""The bounded header probe: the one place that looks at untrusted bytes.

Multimodal design §4.2 and §12.2. Under option 1 the server does exactly two
things with an upload it does not trust -- stream it into a sealed container
while counting it, and read magic bytes at fixed offsets to compare against the
declaration. The second is this module, and the design calls it the only
"parse untrusted input" code in the design, so §5.1 applies with full force:
the failure shapes are designed first and each one is a test.

The design names those shapes verbatim -- "截断、超长、声明与实际不符、magic
合法但后续不完整" -- and requires "固定偏移、有界、不按声明长度分配内存、不派生
文件或子进程、失败即拒绝". Each of those five constraints has a test below, and
so does each of the four shapes. A refusal is asserted by its *reason* rather
than by the exception type alone, because "rejected" and "rejected for the
reason we designed" are different claims and only the second is a property.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from personal_agent.media import probe as probe_module
from personal_agent.media.probe import (
    PROBE_BYTES,
    REASON_DECLARED_INVALID,
    REASON_DECLARED_MISMATCH,
    REASON_DECLARED_NOT_ALLOWED,
    REASON_EMPTY,
    REASON_INCOMPLETE,
    REASON_MALFORMED,
    REASON_OVER_LONG,
    REASON_TRUNCATED,
    REASON_UNKNOWN_MAGIC,
    ProbeError,
    probe_header,
    sniff_mime,
)

JPEG = "image/jpeg"
PNG = "image/png"

# A real JFIF header, truncated to the probe's bound.
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 12
# PNG signature followed by the start of a well-formed IHDR chunk.
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 16

# The round's policy. Only JPEG is produced by the client under option 1, so
# only JPEG is accepted -- but that is configuration (§4.3), which is why the
# probe demands the set rather than holding one.
ALLOWED = frozenset({JPEG})


def refuse(prefix: bytes, *, declared: object, allowed=ALLOWED) -> str:
    with pytest.raises(ProbeError) as excinfo:
        probe_header(prefix, declared_mime=declared, allowed_mimes=allowed)
    return excinfo.value.reason


# --- the four failure shapes the design names -------------------------------


def test_a_truncated_prefix_is_refused() -> None:
    # "截断": not even enough bytes to name the format. A probe that matched on
    # a prefix of the magic would accept any file that starts with FF.
    assert refuse(b"", declared=JPEG) == REASON_EMPTY
    assert refuse(b"\xff", declared=JPEG) == REASON_TRUNCATED


def test_a_legal_magic_with_an_incomplete_body_is_refused() -> None:
    # "magic 合法但后续不完整": the format *is* named -- `ff d8` is SOI and the
    # PNG signature is eight unambiguous bytes -- but the structure the format
    # promises behind the magic is not there yet. The next byte of a real JPEG
    # is always ff (a marker or a fill byte), and a real PNG has an IHDR chunk.
    assert refuse(b"\xff\xd8", declared=JPEG) == REASON_INCOMPLETE
    assert refuse(b"\x89PNG\r\n\x1a\n", declared=PNG, allowed={PNG}) == REASON_INCOMPLETE


def test_a_landmark_that_is_present_and_wrong_is_malformed() -> None:
    # The same design shape, at the point where it stops being recoverable: a
    # third byte of 00 is wrong at a fixed offset, and no byte that arrives
    # later can reach back and change it.
    assert refuse(b"\xff\xd8\x00", declared=JPEG) == REASON_MALFORMED
    assert refuse(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rJUNK", declared=PNG, allowed={PNG}) == (
        REASON_MALFORMED
    )


def test_a_refusal_says_whether_more_bytes_could_still_change_it() -> None:
    # The distinction a streaming caller cannot do without: the first batch of
    # a valid JPEG is `ff d8`, which must not be a refusal, while `ff d8 00` is
    # already beyond rescue. Probing is pure, so re-probing a longer prefix is
    # always allowed; only a non-provisional reason is final.
    def provisional(prefix: bytes, *, declared: object = JPEG, allowed=ALLOWED) -> bool:
        with pytest.raises(ProbeError) as excinfo:
            probe_header(prefix, declared_mime=declared, allowed_mimes=allowed)
        return excinfo.value.provisional

    assert provisional(b"") is True
    assert provisional(b"\xff") is True
    assert provisional(b"\xff\xd8") is True
    assert provisional(b"\xff\xd8\x00") is False
    assert provisional(b"\x00") is False
    # Declared and allowed correctly, and still unknown: the first byte alone
    # rules out every format the probe can name.
    assert provisional(b"GIF89a", declared="image/gif", allowed={"image/gif"}) is False

    # ...and the same prefix, once enough bytes have arrived, stops refusing.
    assert probe_header(JPEG_HEAD, declared_mime=JPEG, allowed_mimes=ALLOWED) == JPEG


def test_a_declaration_that_contradicts_the_bytes_is_refused() -> None:
    # "声明与实际不符". The declaration is never taken on trust: the returned
    # type is always the sniffed one.
    assert refuse(PNG_HEAD, declared=JPEG) == REASON_DECLARED_MISMATCH
    assert (
        refuse(JPEG_HEAD, declared=PNG, allowed={JPEG, PNG})
        == REASON_DECLARED_MISMATCH
    )


def test_an_over_long_prefix_is_refused() -> None:
    # "超长". The bound is the caller's contract too: a probe that accepted an
    # unbounded buffer would be one refactor away from scanning one.
    assert len(JPEG_HEAD) < PROBE_BYTES
    assert refuse(JPEG_HEAD + b"\x00" * PROBE_BYTES, declared=JPEG) == REASON_OVER_LONG


def test_an_unrecognised_magic_is_refused() -> None:
    # Declared correctly for a format nobody supports: the bytes decide.
    assert refuse(b"GIF89a" + b"\x00" * 8, declared="image/gif") == (
        REASON_DECLARED_NOT_ALLOWED
    )
    assert refuse(b"not an image at all", declared=JPEG) == REASON_UNKNOWN_MAGIC


# --- the whitelist is policy, and the probe only enforces it ----------------


def test_the_allowed_set_comes_from_the_caller() -> None:
    # §4.3: limits are versioned configuration and images stay off without it.
    # A probe with a built-in whitelist would be a policy nobody can turn off.
    assert refuse(JPEG_HEAD, declared=JPEG, allowed=frozenset()) == (
        REASON_DECLARED_NOT_ALLOWED
    )
    assert probe_header(JPEG_HEAD, declared_mime=JPEG, allowed_mimes=ALLOWED) == JPEG


def test_a_declaration_outside_the_allowed_set_is_refused_before_the_bytes_are() -> None:
    # Order matters for the log: an unsupported type is refused as unsupported
    # even when the bytes are also wrong, so the operator sees the real reason.
    assert refuse(PNG_HEAD, declared=PNG) == REASON_DECLARED_NOT_ALLOWED


@pytest.mark.parametrize(
    "declared",
    [
        "",
        None,
        42,
        b"image/jpeg",
        "image/jpeg; charset=binary",
        "image/jpeg;q=1",
        " image/jpeg",
        "image/jpeg ",
        "image/jpeg\n",
        "image//jpeg",
    ],
)
def test_a_malformed_declaration_is_refused_rather_than_repaired(declared: object) -> None:
    # §5.1 forbids silently repairing input. Trimming or dropping the
    # parameters would let a client's malformed declaration pass as a good one.
    assert refuse(JPEG_HEAD, declared=declared) == REASON_DECLARED_INVALID


def test_a_declared_type_is_compared_case_insensitively() -> None:
    # Media types are case-insensitive (RFC 2045 §5.1). Lower-casing is the
    # comparison the standard prescribes, not a repair.
    assert probe_header(JPEG_HEAD, declared_mime="IMAGE/JPEG", allowed_mimes=ALLOWED) == JPEG


# --- fixed offset, bounded, no allocation from a declared length ------------


def test_only_the_fixed_offsets_are_consulted() -> None:
    # A PNG signature buried later in the buffer must not change the verdict,
    # and neither must any other trailing content: the magic is at offset 0.
    buried = JPEG_HEAD[:8] + PNG_HEAD[:16]
    assert len(buried) <= PROBE_BYTES
    assert probe_header(buried, declared_mime=JPEG, allowed_mimes=ALLOWED) == JPEG


def test_a_declared_segment_length_is_never_honoured() -> None:
    # "不按声明长度分配内存". This APP0 segment declares 65535 bytes while the
    # buffer holds twenty. A probe that read the field would want them; this one
    # has no declared-size parameter to allocate from in the first place.
    declaring_much = b"\xff\xd8\xff\xe0\xff\xff" + b"\x00" * 20
    assert probe_header(declaring_much, declared_mime=JPEG, allowed_mimes=ALLOWED) == JPEG


def test_a_prefix_of_exactly_the_bound_is_accepted() -> None:
    at_bound = JPEG_HEAD + b"\x00" * (PROBE_BYTES - len(JPEG_HEAD))
    assert len(at_bound) == PROBE_BYTES
    assert probe_header(at_bound, declared_mime=JPEG, allowed_mimes=ALLOWED) == JPEG


def test_the_sniffer_reports_nothing_it_does_not_know() -> None:
    assert sniff_mime(JPEG_HEAD) == JPEG
    assert sniff_mime(PNG_HEAD) == PNG
    assert sniff_mime(b"") is None
    assert sniff_mime(b"\xff\xd8") is None
    assert sniff_mime(b"BM" + b"\x00" * 10) is None


# --- the structural half, which no input can demonstrate --------------------


def test_the_probe_derives_no_file_and_no_subprocess() -> None:
    # "不派生文件或子进程". Unobservable from a call, so it is asserted against
    # the source: the module reaches for no filesystem, no process and no image
    # library, and has nothing it could allocate a buffer with.
    tree = ast.parse(Path(probe_module.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert imported <= {"__future__", "collections.abc", "dataclasses", "typing"}

    forbidden = {"open", "exec", "eval", "compile", "__import__", "bytearray", "system"}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not (called & forbidden)
