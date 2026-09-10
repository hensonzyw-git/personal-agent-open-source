"""The sealed container refuses every way an attacker can rearrange it.

Multimodal design §5.1 requires the reader to verify contiguous indexes, the
sealed chunk count, the sealed byte count and the whole-stream hash, and to
refuse missing, reordered, duplicated, extra and unsealed input. §5.1 also
says the reader must fail closed everywhere rather than truncate or repair.

The failure shapes are written here as tests before the reader exists, because
this is the one place in the design that parses bytes it did not produce. A
green happy path would prove nothing: a reader that returns the right answer
for a well-formed container and a *plausible* answer for a mangled one passes
the happy path too.
"""

from __future__ import annotations

import json

import pytest

from personal_agent.media.container import (
    ContainerError,
    CONTAINER_MAGIC,
    CONTAINER_VERSION,
    MAX_CHUNK_BYTES,
    ChunkHasher,
    open_container,
    read_container,
    write_container,
)
from personal_agent_core.crypto import KeyRing, generate_key

CHUNK = 64
MEDIA = "11111111-1111-4111-8111-111111111111"
ATTEMPT = 1


@pytest.fixture()
def keyring() -> KeyRing:
    return KeyRing([generate_key("k1")], service="personal_agent")


def write(
    tmp_path,
    payload: bytes,
    *,
    keyring,
    chunk_bytes: int = CHUNK,
    name: str = "upload.part",
):
    path = tmp_path / name
    seal = write_container(
        path,
        [payload[i : i + chunk_bytes] for i in range(0, len(payload), chunk_bytes)]
        or [b""],
        keyring=keyring,
        media_id=MEDIA,
        attempt_number=ATTEMPT,
        chunk_bytes=chunk_bytes,
    )
    return path, seal


def reseal_chunks(path, replacements, *, keyring, media_id=MEDIA, attempt=ATTEMPT):
    """Rewrite a container's ciphertext chunks, defeating the GCM tag.

    Used to build the tampering cases a real attacker would need: the container
    is not a signature over itself, so its own bytes are the attack surface.
    """
    raw = path.read_bytes()
    header = len(CONTAINER_MAGIC) + 1
    out = bytearray(raw[:header])
    for record in replacements(raw, header):
        out += record
    path.write_bytes(bytes(out))


# --- the happy path, as the baseline the failures are measured against ------


def test_a_round_trip_returns_the_exact_bytes(tmp_path, keyring) -> None:
    payload = b"the quick brown fox" * 9
    path, seal = write(tmp_path, payload, keyring=keyring)

    assert read_container(
        path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
    ) == payload
    assert seal.total_bytes == len(payload)
    assert seal.chunk_count == (len(payload) + CHUNK - 1) // CHUNK


def test_an_empty_payload_round_trips_as_one_empty_chunk(tmp_path, keyring) -> None:
    # An empty upload is not a legitimate image, but the container must still
    # have a defined shape for it so "empty" and "missing" stay distinguishable.
    path, seal = write(tmp_path, b"", keyring=keyring)
    assert read_container(
        path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
    ) == b""
    assert seal.chunk_count == 1


def test_the_whole_stream_hash_is_over_the_plaintext(tmp_path, keyring) -> None:
    import hashlib

    payload = b"abcdefgh" * 40
    _, seal = write(tmp_path, payload, keyring=keyring)
    assert seal.sha256 == hashlib.sha256(payload).hexdigest()


# --- the failure shapes ----------------------------------------------------


def test_a_truncated_file_is_refused(tmp_path, keyring) -> None:
    payload = b"x" * (CHUNK * 3)
    path, seal = write(tmp_path, payload, keyring=keyring)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_truncated_final_chunk_is_refused(tmp_path, keyring) -> None:
    payload = b"x" * (CHUNK * 2)
    path, seal = write(tmp_path, payload, keyring=keyring)
    path.write_bytes(path.read_bytes()[:-8])

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_missing_chunk_is_refused(tmp_path, keyring) -> None:
    payload = bytes(range(256)) * 2
    path, seal = write(tmp_path, payload, keyring=keyring)

    def drop_first(raw: bytes, header: int):
        records = []
        offset = header
        while offset < len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            records.append(raw[offset : offset + 4 + length])
            offset += 4 + length
        return records[1:]

    reseal_chunks(path, drop_first, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_reordered_chunks_are_refused(tmp_path, keyring) -> None:
    # Reordering is refused because each chunk's AAD names its own index, so a
    # moved chunk fails authentication rather than decrypting into the wrong
    # place in the stream.
    payload = bytes(range(256)) * 4
    path, seal = write(tmp_path, payload, keyring=keyring)

    def swap_first_two(raw: bytes, header: int):
        records = []
        offset = header
        while offset < len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            records.append(raw[offset : offset + 4 + length])
            offset += 4 + length
        records[0], records[1] = records[1], records[0]
        return records

    reseal_chunks(path, swap_first_two, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_duplicated_chunk_is_refused(tmp_path, keyring) -> None:
    payload = bytes(range(256)) * 2
    path, seal = write(tmp_path, payload, keyring=keyring)

    def duplicate_first(raw: bytes, header: int):
        records = []
        offset = header
        while offset < len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            records.append(raw[offset : offset + 4 + length])
            offset += 4 + length
        return [records[0], records[0], *records[1:]]

    reseal_chunks(path, duplicate_first, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_an_extra_trailing_chunk_is_refused(tmp_path, keyring) -> None:
    payload = bytes(range(256)) * 2
    path, seal = write(tmp_path, payload, keyring=keyring)

    def append_last(raw: bytes, header: int):
        records = []
        offset = header
        while offset < len(raw):
            length = int.from_bytes(raw[offset : offset + 4], "big")
            records.append(raw[offset : offset + 4 + length])
            offset += 4 + length
        return [*records, records[-1]]

    reseal_chunks(path, append_last, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_flipped_ciphertext_bit_is_refused(tmp_path, keyring) -> None:
    payload = b"y" * (CHUNK * 2)
    path, seal = write(tmp_path, payload, keyring=keyring)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0x01
    path.write_bytes(bytes(raw))

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_chunk_from_another_object_is_refused(tmp_path, keyring) -> None:
    # The AAD binds media_id, so a valid chunk lifted from a different upload
    # does not decrypt here.
    other = "22222222-2222-4222-8222-222222222222"
    payload = b"z" * (CHUNK * 2)
    other_path = tmp_path / "other.part"
    write_container(
        other_path,
        [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)],
        keyring=keyring,
        media_id=other,
        attempt_number=1,
        chunk_bytes=CHUNK,
    )
    path, seal = write(tmp_path, payload, keyring=keyring)

    donor = other_path.read_bytes()
    header = len(CONTAINER_MAGIC) + 1
    record_length = int.from_bytes(donor[header : header + 4], "big")
    donor_record = donor[header : header + 4 + record_length]
    own = path.read_bytes()
    own_record_length = int.from_bytes(own[header : header + 4], "big")
    path.write_bytes(
        own[:header]
        + donor_record
        + own[header + 4 + own_record_length :]
    )

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_chunk_from_another_attempt_of_the_same_object_is_refused(
    tmp_path, keyring
) -> None:
    # Attempts are separate owners of separate staging files; adopting one
    # attempt's chunk into another's stream must not be possible, or a failed
    # attempt could poison a later one.
    payload = b"z" * (CHUNK * 2)
    other_path = tmp_path / "attempt2.part"
    write_container(
        other_path,
        [payload[i : i + CHUNK] for i in range(0, len(payload), CHUNK)],
        keyring=keyring,
        media_id=MEDIA,
        attempt_number=2,
        chunk_bytes=CHUNK,
    )
    path, seal = write(tmp_path, payload, keyring=keyring)

    donor = other_path.read_bytes()
    header = len(CONTAINER_MAGIC) + 1
    record_length = int.from_bytes(donor[header : header + 4], "big")
    donor_record = donor[header : header + 4 + record_length]
    own = path.read_bytes()
    own_record_length = int.from_bytes(own[header : header + 4], "big")
    path.write_bytes(
        own[:header] + donor_record + own[header + 4 + own_record_length :]
    )

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_chunk_sealed_under_another_role_is_refused(tmp_path, keyring) -> None:
    # §5.1 requires the AAD to bind the file role. Without it, an object of one
    # kind could be read back as another.
    path = tmp_path / "role.part"
    seal = write_container(
        path,
        [b"r" * CHUNK],
        keyring=keyring,
        media_id=MEDIA,
        attempt_number=ATTEMPT,
        role="chat_image",
        chunk_bytes=CHUNK,
    )

    with pytest.raises(ContainerError):
        read_container(
            path,
            seal,
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
            role="some_other_role",
        )


def test_the_same_role_round_trips(tmp_path, keyring) -> None:
    path = tmp_path / "role.part"
    seal = write_container(
        path,
        [b"s" * CHUNK],
        keyring=keyring,
        media_id=MEDIA,
        attempt_number=ATTEMPT,
        role="other_purpose",
        chunk_bytes=CHUNK,
    )
    assert (
        read_container(
            path,
            seal,
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
            role="other_purpose",
        )
        == b"s" * CHUNK
    )


def test_a_role_cannot_smuggle_a_separator(tmp_path, keyring) -> None:
    # The row id is colon-joined, so a role containing a colon could forge
    # another object's or attempt's coordinates.
    for bad in ("chat:image", "", None, 7):
        with pytest.raises(ContainerError):
            write_container(
                tmp_path / "bad.part",
                [b"x"],
                keyring=keyring,
                media_id=MEDIA,
                attempt_number=ATTEMPT,
                role=bad,
                chunk_bytes=CHUNK,
            )


def test_a_swapped_seal_from_another_stream_is_refused(tmp_path, keyring) -> None:
    # The seal record is sealed too, but it is also the thing that says how
    # many chunks to expect; a seal that describes a different stream must not
    # make this one look complete.
    path, _ = write(tmp_path, b"a" * (CHUNK * 2), keyring=keyring)
    _, other_seal = write(
        tmp_path, b"b" * (CHUNK * 3), keyring=keyring, name="other.part"
    )

    with pytest.raises(ContainerError):
        read_container(
            path, other_seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_an_unsealed_container_is_refused(tmp_path, keyring) -> None:
    path, _ = write(tmp_path, b"c" * CHUNK, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(path, None, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT)


def test_a_wrong_format_version_is_refused(tmp_path, keyring) -> None:
    path, seal = write(tmp_path, b"d" * CHUNK, keyring=keyring)
    raw = bytearray(path.read_bytes())
    raw[len(CONTAINER_MAGIC)] = CONTAINER_VERSION + 1
    path.write_bytes(bytes(raw))

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_foreign_magic_is_refused(tmp_path, keyring) -> None:
    path, seal = write(tmp_path, b"e" * CHUNK, keyring=keyring)
    path.write_bytes(b"NOTMEDIA" + path.read_bytes()[len(CONTAINER_MAGIC) :])

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_header_only_file_is_refused(tmp_path, keyring) -> None:
    path, seal = write(tmp_path, b"f" * CHUNK, keyring=keyring)
    path.write_bytes(CONTAINER_MAGIC + bytes([CONTAINER_VERSION]))

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_an_over_long_chunk_length_is_refused_without_allocating(tmp_path, keyring) -> None:
    # §5.4: the reader must not allocate from a length it was told. A 4 GiB
    # declared chunk must be a refusal on the length alone, before any read.
    path, seal = write(tmp_path, b"g" * CHUNK, keyring=keyring)
    raw = path.read_bytes()
    header = len(CONTAINER_MAGIC) + 1
    path.write_bytes(
        raw[:header] + (MAX_CHUNK_BYTES + 1).to_bytes(4, "big") + raw[header + 4 :]
    )

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_negative_or_zero_length_record_is_refused(tmp_path, keyring) -> None:
    path, seal = write(tmp_path, b"h" * CHUNK, keyring=keyring)
    raw = path.read_bytes()
    header = len(CONTAINER_MAGIC) + 1
    path.write_bytes(raw[:header] + (0).to_bytes(4, "big") + raw[header + 4 :])

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_chunk_larger_than_the_configured_bound_is_refused(tmp_path, keyring) -> None:
    payload = b"i" * (CHUNK * 2)
    path, seal = write(tmp_path, payload, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(
            path,
            seal,
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
            chunk_bytes=CHUNK // 2,
        )


def test_a_stream_longer_than_the_declared_size_is_refused(tmp_path, keyring) -> None:
    # A reader that stops at the sealed byte count would return a prefix and
    # call it the image. The container must refuse the extra bytes instead.
    path, seal = write(tmp_path, b"j" * (CHUNK * 2), keyring=keyring)
    raw = path.read_bytes()
    path.write_bytes(raw + raw[-1:])

    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_directory_in_place_of_the_container_is_refused(tmp_path, keyring) -> None:
    _, seal = write(tmp_path, b"k" * CHUNK, keyring=keyring)
    path = tmp_path / "upload.part"
    path.unlink()
    path.mkdir()
    with pytest.raises(ContainerError):
        read_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_symlink_in_place_of_the_container_is_refused(tmp_path, keyring) -> None:
    real, seal = write(tmp_path, b"l" * CHUNK, keyring=keyring)
    link = tmp_path / "link.part"
    link.symlink_to(real)

    with pytest.raises(ContainerError):
        read_container(
            link, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )


def test_a_missing_file_is_refused(tmp_path, keyring) -> None:
    _, seal = write(tmp_path, b"m" * CHUNK, keyring=keyring)
    with pytest.raises(ContainerError):
        read_container(
            tmp_path / "absent.part",
            seal,
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
        )


def test_an_extremely_large_file_is_refused_before_being_read(tmp_path, keyring) -> None:
    # The bound is on the stream, not on the seal: a file that is obviously
    # past the ceiling is refused on its size rather than after being read.
    path, seal = write(tmp_path, b"n" * CHUNK, keyring=keyring)
    with open(path, "ab") as handle:
        handle.write(b"\x00" * (MAX_CHUNK_BYTES * 2))

    with pytest.raises(ContainerError):
        read_container(
            path,
            seal,
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
            max_content_bytes=MAX_CHUNK_BYTES,
        )


# --- the writer's own bindings ---------------------------------------------


def test_the_writer_refuses_a_chunk_over_the_bound(tmp_path, keyring) -> None:
    with pytest.raises(ContainerError):
        write_container(
            tmp_path / "big.part",
            [b"x" * (MAX_CHUNK_BYTES + 1)],
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
            chunk_bytes=MAX_CHUNK_BYTES,
        )


def test_the_writer_refuses_a_media_id_that_is_not_a_uuid(tmp_path, keyring) -> None:
    # The media id becomes an AAD row id and, elsewhere, a path component. A
    # caller-supplied `../` must not be able to reach either.
    with pytest.raises(ContainerError):
        write_container(
            tmp_path / "bad.part",
            [b"x"],
            keyring=keyring,
            media_id="../../etc/passwd",
            attempt_number=ATTEMPT,
            chunk_bytes=CHUNK,
        )


def test_the_writer_refuses_a_non_positive_attempt_number(tmp_path, keyring) -> None:
    with pytest.raises(ContainerError):
        write_container(
            tmp_path / "bad.part",
            [b"x"],
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=0,
            chunk_bytes=CHUNK,
        )


def test_the_writer_refuses_a_chunk_that_exceeds_the_declared_chunk_size(
    tmp_path, keyring
) -> None:
    with pytest.raises(ContainerError):
        write_container(
            tmp_path / "bad.part",
            [b"x" * (CHUNK + 1)],
            keyring=keyring,
            media_id=MEDIA,
            attempt_number=ATTEMPT,
            chunk_bytes=CHUNK,
        )


# --- the running hash the upload path needs --------------------------------


def test_the_hasher_reports_running_totals() -> None:
    hasher = ChunkHasher()
    hasher.update(b"abc")
    hasher.update(b"def")
    assert hasher.total_bytes == 6
    assert hasher.chunk_count == 2
    assert hasher.hexdigest() == (
        "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721"
    )


def test_a_container_serialises_its_envelope_as_json(tmp_path, keyring) -> None:
    # The chunk record is JSON so the format is readable by a restore drill
    # without the implementation, the same way the sealed columns are.
    path, _ = write(tmp_path, b"o" * CHUNK, keyring=keyring)
    raw = path.read_bytes()
    header = len(CONTAINER_MAGIC) + 1
    length = int.from_bytes(raw[header : header + 4], "big")
    envelope = json.loads(raw[header + 4 : header + 4 + length])
    assert set(envelope) == {"v", "kid", "nonce", "ciphertext", "tag"}


def test_open_container_yields_chunks_in_order(tmp_path, keyring) -> None:
    payload = bytes(range(256)) * 3
    path, seal = write(tmp_path, payload, keyring=keyring)

    streamed = b"".join(
        open_container(
            path, seal, keyring=keyring, media_id=MEDIA, attempt_number=ATTEMPT
        )
    )
    assert streamed == payload
