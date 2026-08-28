"""`DAL-028`: patch policy scanner — the model-patch boundary (R09-A1).

Before a model-produced patch may become a candidate commit, it is judged
against a closed rule pipeline: leaked secret material, protected paths,
out-of-bounds files, binary payloads and abnormal patch size. The frozen
contract line is "检查越界文件、secret、binary、异常大小和受保护路径"; this
module is the pure decision half, in the same family as ``path.py`` and
``secret_output.py``. It carries no persistence and no I/O — the caller owns
gathering the patch facts and applying the ``block_feature`` transition that
consumes the ``POLICY_FAILURE`` result.

Trusted-facts contract. The scanner is a pure judge over facts the trusted
controller derives from Git and the approved plan: ``change_type``,
``is_binary`` and ``size_bytes`` must come from Git (diff/ls-files), never
from provider self-report — a provider that could name its own file binary,
deleted, or undersized would defeat the binary/size/delete exemptions. For
non-binary added or modified files the declared ``size_bytes`` is cross-checked
against ``len(content.encode())``; a mismatch is ``INVALID_ARGUMENT``. For
binary entries the content is empty by definition, so the size cannot be
cross-checked here — that is the one size fact the controller vouches for.

Rule pipeline order is a safety property, not a style choice (CLAUDE.md §5.2):
leak checks run **before** fact/semantic checks, and they run over both the
path and the content — a credential parked in a filename with clean content
must not pass. The stages are evaluated in a fixed order and the first stage
with any hit wins (this also makes attribution unambiguous: a verdict for a
later stage implies every earlier stage was clean):

1. ``leak_fingerprint`` — a configured secret fingerprint (exact canary or
   known-credential value) appears verbatim in a file's path or content;
2. ``leak_structural`` — high-signal structural key material (PEM markers,
   including the encrypted form; provider key shapes under an explicit prefix
   table, with delimiter-bearing tails like ``sk-proj-…`` handled by their own
   prefix entries);
3. ``protected_path`` — a protected pattern: component-boundary trees
   (``deploy``, ``config``, the key-inventory doc prefix) and deliberate
   wide matches on path components (any ``.git*`` component, any ``.env``
   basename);
4. ``out_of_bounds`` — the path is not inside the approved plan's allowed
   paths. Matching follows the frozen ``allowed_paths`` semantics
   (DAL021-024 §plan: fields exactly ``path,path_type``): a ``file`` entry
   matches only that exact path; a ``directory`` entry matches itself and
   everything beneath it at a component boundary. An ``out_of_bounds`` verdict
   therefore never fires on a file-type authorisation's neighbour paths;
5. ``binary`` — an added or modified file is binary (an unreviewable payload
   that can also carry a secret past the text scan);
6. ``size`` — an added or modified file exceeds the per-file size policy;
7. ``size_aggregate`` — the total added/modified volume exceeds the patch
   aggregate size policy;
8. ``file_count`` — the patch touches more files than the count policy.

The verdict names the winning rule, the offending file's **index** in the
facts list, and — only for non-leak rules — the file's path. Leak-stage
verdicts deliberately omit the path: if the secret sits in the filename, the
raw path itself is sensitive material and echoing it would turn the deny
verdict into a new leak channel (CLAUDE.md §5 forbids printing secrets in
logs, snapshots or replies). The index is positional metadata and is always
safe to surface.

Policy constants below were **frozen on 2026-08-28** by Henson's decision
(adopting the round-3 review recommendation): per-file 512 KiB, aggregate
1 MiB, file-count 50, and the four-part protected-path table. The Roadmap
froze the *categories* (异常大小、受保护路径); these numbers close it. They
remain named, grouped and documented so any future change is a reviewed
one-line edit.

This module deliberately declares no operation spec id. It is a stage-internal
guard, not a dispatchable operation; if it is later promoted into the frozen
operation registry, that registration happens through the manifest tooling
with an explicit authorisation, never by an unregistered spec id in code.

Facts schema: ``dal.patch-scan-facts/1.0`` (closed shape; unknown fields,
wrong types, non-normalized paths, size/content mismatches and duplicate
paths are ``INVALID_ARGUMENT``, never silently repaired).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from personal_agent_dal.errors import DalError, DalErrorCode

#: The reason any rule hit blocks with, matching the frozen
#: ``BLK-POLICY--coding`` transition's ``result_reason_code``.
BLOCK_REASON: Final[str] = "POLICY_FAILURE"

FACTS_SCHEMA_VERSION: Final[str] = "dal.patch-scan-facts/1.0"

#: The closed rule vocabulary the verdict's ``rule`` field draws from, in
#: pipeline order. ``leak_*`` stages precede every fact/semantic stage (§5.2).
RULE_ORDER: Final[tuple[str, ...]] = (
    "leak_fingerprint",
    "leak_structural",
    "protected_path",
    "out_of_bounds",
    "binary",
    "size",
    "size_aggregate",
    "file_count",
)

# --- Size policy (frozen 2026-08-28 by Henson's decision; see module docstring).

#: Maximum size of a single added or modified file.
MAX_PATCH_FILE_SIZE_BYTES: Final[int] = 512 * 1024
#: Maximum total size of all added or modified files in one patch.
MAX_PATCH_TOTAL_SIZE_BYTES: Final[int] = 1024 * 1024
#: Maximum number of files touched by one patch (deletions included).
MAX_PATCH_FILE_COUNT: Final[int] = 50

# --------------------------------------------------------------------------

#: Structural private-key markers (substring, case-sensitive). The dashes and
#: surrounding text vary; the marker text is standardised.
PEM_MARKERS: Final[tuple[str, ...]] = (
    "BEGIN RSA PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN ENCRYPTED PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN DSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN PGP PRIVATE KEY BLOCK",
)

#: Provider key grammar: ``(prefix, min_total_tail, min_alnum_tail)``. A hit
#: requires the character before the prefix to be **non-alphanumeric**:
#: alphanumeric-adjacent text (``Xsk-…``, as in ``KEYXsk-…``) does not arm
#: the shape, while every delimiter left of the prefix — including ``-`` and
#: ``_`` — leaves it armed (``leaked_sk-a1b2…`` hits; a ``task-sk-…`` compound
#: with a key-like tail is an accepted false positive). The tail is a run of
#: ``[A-Za-z0-9_-]`` with at least ``min_total_tail`` characters of which at
#: least ``min_alnum_tail`` are alphanumeric. There is deliberately no
#: digit/entropy gate: a structural rule that assumes "real keys almost all
#: contain digits" leans on an unfrozen probabilistic assumption, and a
#: digit-less key would sail through. Longer prefixes are listed first so a
#: specific shape (``sk-proj-``, whose tail carries separators) wins
#: attribution over the generic ``sk-`` entry. The rule is high-recall by
#: design: a structural hit is a leak *suspect* that blocks and routes to a
#: human, and a prose-like long tail (``sk-this-is-a-very-long-sentence``)
#: is an accepted false positive; the fail direction is deny.
KEY_PREFIXES: Final[tuple[tuple[str, int, int], ...]] = (
    ("sk-proj-", 20, 16),
    ("sk-ant-", 20, 16),
    ("sk-svcacct-", 20, 16),
    ("sk-", 16, 16),
    ("gsk_", 20, 20),  # Zhipu GLM
    ("AKIA", 16, 16),  # AWS access key ids
    ("ghp_", 20, 20),  # GitHub personal access tokens
    ("github_pat_", 20, 20),  # GitHub fine-grained tokens
)

#: Component-boundary protected trees: a path is protected when it equals the
#: entry or sits beneath it at a component boundary (``config`` never touches
#: ``configuration.py``).
PROTECTED_TREES: Final[tuple[str, ...]] = (
    "deploy",
    "config",
)

#: Document-name protected prefixes: matched against the file **basename** so
#: the key-inventory document and any siblings are covered without marking all
#: of ``docs/`` protected.
PROTECTED_DOC_PREFIXES: Final[tuple[str, ...]] = ("密钥清单",)

#: Wide component matches, deliberate exceptions to component-boundary
#: matching: any path component starting with ``.git`` (covers ``.git``,
#: ``.github``, ``.gitignore``, ``.gitmodules``) and any ``.env`` basename.
PROTECTED_COMPONENT_PREFIX: Final[str] = ".git"
ENV_BASENAMES: Final[tuple[str, ...]] = (".env",)

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FILE_FIELDS: Final[frozenset[str]] = frozenset(
    {"path", "change_type", "size_bytes", "is_binary", "content"}
)
ALLOWED_PATH_FIELDS: Final[frozenset[str]] = frozenset({"path", "path_type"})
FACTS_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "target", "allowed_paths", "secret_fingerprints", "files"}
)

#: The frozen states whose transitions produce a patch (SM-PROVIDER-DONE,
#: SM-FIX-DONE). Any other state is not a patch-scan scenario.
PATCH_STATES: Final[frozenset[str]] = frozenset({"coding", "fixing"})
CHANGE_TYPES: Final[frozenset[str]] = frozenset({"add", "modify", "delete"})
PATH_TYPES: Final[frozenset[str]] = frozenset({"file", "directory"})

_KEY_TAIL_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_ALNUM: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


@dataclass(frozen=True)
class PatchPolicyEvaluation:
    """The pure verdict of the patch policy scan.

    ``conflict`` is True when any rule in the pipeline hit; ``rule`` names the
    winning stage (first in ``RULE_ORDER``); ``file_index`` is the offending
    file's position in the facts list (None for patch-level rules such as
    ``file_count``); ``path`` mirrors the offending file's path **only for
    non-leak rules** — leak-stage hits leave it unset because the path itself
    may be the sensitive material. On a clean patch all fields are unset.
    The verdict never carries matched content or fingerprint values.
    """

    conflict: bool
    rule: str | None = None
    file_index: int | None = None
    path: str | None = None


def _invalid(detail: str) -> DalError:
    return DalError(DalErrorCode.INVALID_ARGUMENT, internal_detail=detail)


def _validate_str(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field} must be a non-empty string")
    return value


def _validate_int(value: Any, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _invalid(f"{field} must be a non-negative integer")
    return value


def _normalized_repo_path(value: Any, *, field: str) -> str:
    """Validate a canonical repo-relative POSIX path (DAL021-024 §plan).

    Non-empty, not absolute, no ``.``/``..`` segment, no empty segment
    (``//``, leading/trailing slash) — a path that fails normalization is a
    shape error, not a rule hit: Git never produces such paths, so only a
    malformed fact or a crafted patch carries one.
    """
    _validate_str(value, field=field)
    if value.startswith("/"):
        raise _invalid(f"{field} must be repo-relative, not absolute")
    components = value.split("/")
    if any(component in (".", "..") for component in components):
        raise _invalid(f"{field} must not contain . or .. segments")
    if any(component == "" for component in components):
        raise _invalid(f"{field} must be a normalized path without empty segments")
    return value


def _validate_facts(facts: dict[str, Any]) -> None:
    if frozenset(facts) != FACTS_FIELDS:
        raise _invalid("patch scan facts shape is not closed")
    if facts.get("schema_version") != FACTS_SCHEMA_VERSION:
        raise _invalid("wrong patch scan facts schema")

    target = facts.get("target")
    if not isinstance(target, dict) or frozenset(target) != TARGET_FIELDS:
        raise _invalid("target shape is not closed")
    if (
        not isinstance(target.get("entity_id"), str)
        or not target["entity_id"]
        or target.get("entity_type") != "feature"
        or target.get("state") not in PATCH_STATES
        or not isinstance(target.get("version"), int)
        or isinstance(target.get("version"), bool)
        or target["version"] < 0
    ):
        raise _invalid("invalid patch scan target")

    allowed_paths = facts.get("allowed_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths:
        raise _invalid("allowed_paths must be a non-empty list")
    for entry in allowed_paths:
        if not isinstance(entry, dict) or frozenset(entry) != ALLOWED_PATH_FIELDS:
            raise _invalid("allowed path shape is not closed")
        path = _normalized_repo_path(entry.get("path"), field="allowed path")
        if entry.get("path_type") not in PATH_TYPES:
            raise _invalid("allowed path_type must be file or directory")
        # The frozen plan-artifact schema (plan-artifact_schema_v1.0.json)
        # forbids only the exact ``.git`` component; ``.github`` and
        # ``.gitignore`` are expressible in a plan and are then deliberately
        # rejected by the protected-path stage — plan may express, policy
        # denies. Do not re-classify them here as malformed.
        if any(component == ".git" for component in path.split("/")):
            raise _invalid("allowed path must not name a .git subtree")

    fingerprints = facts.get("secret_fingerprints")
    if not isinstance(fingerprints, list) or not fingerprints:
        raise _invalid("secret_fingerprints must be a non-empty list")
    if any(not isinstance(fp, str) or not fp for fp in fingerprints):
        raise _invalid("secret_fingerprints must contain non-empty strings")

    files = facts.get("files")
    if not isinstance(files, list) or not files:
        raise _invalid("files must be a non-empty list")
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict) or frozenset(entry) != FILE_FIELDS:
            raise _invalid("file entry shape is not closed")
        path = _normalized_repo_path(entry.get("path"), field="file path")
        if entry.get("change_type") not in CHANGE_TYPES:
            raise _invalid("unknown change type")
        size = _validate_int(entry.get("size_bytes"), field="file size")
        if not isinstance(entry.get("is_binary"), bool):
            raise _invalid("is_binary must be a boolean")
        content = entry.get("content")
        if not isinstance(content, str):
            raise _invalid("content must be a string")
        if content and entry["is_binary"]:
            raise _invalid("a binary entry carries no inspectable content")
        if entry["change_type"] == "delete" and content:
            raise _invalid("a delete entry carries no content")
        if (
            entry["change_type"] in ("add", "modify")
            and not entry["is_binary"]
            and size != len(content.encode("utf-8"))
        ):
            raise _invalid("size_bytes must match content length for text entries")
        if path in seen:
            raise _invalid("duplicate file path in patch")
        seen.add(path)


def _is_within(path: str, prefix: str) -> bool:
    """Component-boundary containment: ``src/dal`` covers ``src/dal/x.py``
    and ``src/dal`` itself, but never ``src/dalfoo``."""
    return path == prefix or path.startswith(prefix + "/")


def _hits_protected_path(path: str) -> bool:
    if any(_is_within(path, tree) for tree in PROTECTED_TREES):
        return True
    components = path.split("/")
    if any(
        component.startswith(PROTECTED_COMPONENT_PREFIX) for component in components
    ):
        return True
    basename = components[-1]
    if any(basename.startswith(doc) for doc in PROTECTED_DOC_PREFIXES):
        return True
    if basename in ENV_BASENAMES or basename.startswith(".env."):
        return True
    return False


def _inside_allowed(path: str, allowed_paths: list[dict[str, Any]]) -> bool:
    """The frozen allowed-paths semantics (DAL021-024 §plan): a ``file``
    authorisation covers exactly that path; a ``directory`` authorisation
    covers itself and everything beneath it at a component boundary."""
    for entry in allowed_paths:
        if entry["path_type"] == "file":
            if path == entry["path"]:
                return True
        elif _is_within(path, entry["path"]):
            return True
    return False


def _hits_structural_secret(text: str) -> bool:
    for marker in PEM_MARKERS:
        if marker in text:
            return True
    for prefix, min_total, min_alnum in KEY_PREFIXES:
        start = 0
        while True:
            index = text.find(prefix, start)
            if index < 0:
                break
            start = index + 1
            if index > 0 and text[index - 1] in _ALNUM:
                continue  # alphanumeric left boundary: an ordinary word
            tail_total = 0
            tail_alnum = 0
            cursor = index + len(prefix)
            while cursor < len(text) and text[cursor] in _KEY_TAIL_CHARS:
                tail_total += 1
                if text[cursor] in _ALNUM:
                    tail_alnum += 1
                cursor += 1
            if tail_total >= min_total and tail_alnum >= min_alnum:
                return True
    return False


def _file_hits(
    rule: str, entry: dict[str, Any], allowed_paths: list[dict[str, Any]]
) -> bool:
    """Whether one file entry trips a per-file rule stage.

    One uniform signature for every per-file stage — the evaluator calls it
    without adapting arguments per rule (CLAUDE.md §5.2). ``leak_fingerprint``
    is handled by the caller because it also needs the fingerprint list.
    """
    if rule == "leak_structural":
        return _hits_structural_secret(entry["path"]) or _hits_structural_secret(
            entry["content"]
        )
    if rule == "protected_path":
        return _hits_protected_path(entry["path"])
    if rule == "out_of_bounds":
        return not _inside_allowed(entry["path"], allowed_paths)
    if rule == "binary":
        return entry["change_type"] != "delete" and entry["is_binary"]
    if rule == "size":
        return entry["change_type"] != "delete" and (
            entry["size_bytes"] > MAX_PATCH_FILE_SIZE_BYTES
        )
    raise _invalid(f"unknown per-file rule: {rule}")


def _first_hit(
    rule: str,
    files: list[dict[str, Any]],
    allowed_paths: list[dict[str, Any]],
    fingerprints: list[str],
) -> tuple[bool, int | None, str | None]:
    """The first hit of this pipeline stage as ``(hit, file_index, path)``.

    One uniform signature for every stage — the evaluator calls it in
    ``RULE_ORDER`` without adapting arguments per rule (CLAUDE.md §5.2).
    ``file_index``/``path`` are None for patch-level violations (file_count)
    and for leak-stage hits, whose path may itself be the sensitive material.
    """
    if rule == "file_count":
        return (len(files) > MAX_PATCH_FILE_COUNT, None, None)
    if rule == "size_aggregate":
        total = sum(
            entry["size_bytes"]
            for entry in files
            if entry["change_type"] != "delete"
        )
        if total <= MAX_PATCH_TOTAL_SIZE_BYTES:
            return (False, None, None)
        largest = max(
            range(len(files)),
            key=lambda i: (
                files[i]["size_bytes"] if files[i]["change_type"] != "delete" else -1
            ),
        )
        return (True, largest, files[largest]["path"])
    for index, entry in enumerate(files):
        if rule == "leak_fingerprint":
            if any(
                fp in entry["path"] or fp in entry["content"] for fp in fingerprints
            ):
                return (True, index, None)
        elif _file_hits(rule, entry, allowed_paths):
            # Leak-stage hits never echo the path: it may carry the secret.
            return (True, index, None if rule.startswith("leak_") else entry["path"])
    return (False, None, None)


def evaluate_patch_policy(facts: dict[str, Any]) -> PatchPolicyEvaluation:
    """Judge a model-produced patch against the closed rule pipeline.

    Returns the first pipeline stage with a hit as a ``POLICY_FAILURE``
    conflict verdict, or a clean verdict when every stage passes. The verdict
    is pure: it does not move the feature, write audit rows or echo any patch
    content.
    """
    if not isinstance(facts, dict):
        raise _invalid("patch scan facts must be an object")
    _validate_facts(facts)

    for rule in RULE_ORDER:
        hit, file_index, path = _first_hit(
            rule,
            facts["files"],
            facts["allowed_paths"],
            facts["secret_fingerprints"],
        )
        if hit:
            return PatchPolicyEvaluation(
                conflict=True, rule=rule, file_index=file_index, path=path
            )
    return PatchPolicyEvaluation(conflict=False)
