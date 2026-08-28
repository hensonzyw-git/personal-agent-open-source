"""`DAL-028`: patch policy scanner — the model-patch boundary (R09-A1).

Before a model-produced patch may become a candidate commit, it is judged
against a closed rule pipeline: out-of-bounds files, leaked secret material,
binary payloads, abnormal file size and protected paths. The frozen contract
line is "检查越界文件、secret、binary、异常大小和受保护路径"; this module is
the pure decision half, in the same family as ``path.py`` and
``secret_output.py``. It carries no persistence and no I/O — the caller owns
gathering the patch facts and applying the ``block_feature`` transition that
consumes the ``POLICY_FAILURE`` result.

Rule pipeline order is a safety property, not a style choice (CLAUDE.md §5.2):
leak checks run **before** fact/semantic checks. A credential string often
contains numbers, dates or identifiers, so a fact check that ran first would
mis-classify a leaked secret as a path or shape violation, return the wrong
failure code, and hide the real exposure. The stages below are evaluated in a
fixed order and the first stage with any hit wins:

1. ``leak_fingerprint`` — a configured secret fingerprint (exact canary or
   known-credential value) appears verbatim in a file's content;
2. ``leak_structural`` — high-signal structural key material (PEM private-key
   markers; provider key prefixes with a long alphanumeric tail);
3. ``protected_path`` — the path falls under a protected prefix or is a
   dotenv file (``.git``/``.github`` included, ``deploy/``, ``config/``,
   the key inventory, and any ``.env``/``.env.*`` basename);
4. ``out_of_bounds`` — the path is outside every allowed path of the
   approved plan (component-boundary prefix match; an absolute or ``..``
   path can never match and is out of bounds);
5. ``binary`` — an added or modified file is binary (an unreviewable payload
   that can also carry a secret past the text scan);
6. ``size`` — an added or modified file exceeds the size policy.

The verdict names only the rule and the offending path. It never carries the
matched content, the fingerprint value or any patch body: a denial must not
itself become a leak channel.

This module deliberately declares no operation spec id. It is a stage-internal
guard, not a dispatchable operation; if it is later promoted into the frozen
operation registry, that registration happens through the manifest tooling
with an explicit authorisation, never by an unregistered spec id in code.

Facts schema: ``dal.patch-scan-facts/1.0`` (closed shape; unknown fields,
wrong types, duplicate paths and non-empty content on a delete are
``INVALID_ARGUMENT``, never silently repaired).
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
)

#: Structural private-key markers (substring, case-sensitive). The dashes and
#: surrounding text vary; the marker text is standardised.
PEM_MARKERS: Final[tuple[str, ...]] = (
    "BEGIN RSA PRIVATE KEY",
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN DSA PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN PGP PRIVATE KEY BLOCK",
)

#: ``(prefix, minimum_alphanumeric_tail)`` pairs for provider key shapes.
#: A hit requires the character before the prefix to be non-alphanumeric, so
#: ordinary words like ``task-`` or ``risk-`` do not trip ``sk-``.
KEY_PREFIXES: Final[tuple[tuple[str, int], ...]] = (
    ("gsk_", 20),  # Zhipu GLM
    ("sk-", 16),  # DeepSeek / Anthropic / OpenAI shapes
    ("AKIA", 16),  # AWS access key ids
    ("ghp_", 20),  # GitHub personal access tokens
    ("github_pat_", 20),  # GitHub fine-grained tokens
)

#: Protected path prefixes (plain ``startswith``; conservative on purpose —
#: ``.git`` covers ``.github``, ``.gitignore``, ``.gitmodules`` and kin).
PROTECTED_PATH_PREFIXES: Final[tuple[str, ...]] = (
    ".git",
    "deploy",
    "config",
    "docs/密钥清单",
)

#: Size policy for a single added or modified file, in bytes.
MAX_PATCH_FILE_SIZE_BYTES: Final[int] = 512 * 1024

TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"entity_id", "entity_type", "state", "version"}
)
FILE_FIELDS: Final[frozenset[str]] = frozenset(
    {"path", "change_type", "size_bytes", "is_binary", "content"}
)
FACTS_FIELDS: Final[frozenset[str]] = frozenset(
    {"schema_version", "target", "allowed_paths", "secret_fingerprints", "files"}
)

#: The frozen states whose transitions produce a patch (SM-PROVIDER-DONE,
#: SM-FIX-DONE). Any other state is not a patch-scan scenario.
PATCH_STATES: Final[frozenset[str]] = frozenset({"coding", "fixing"})
CHANGE_TYPES: Final[frozenset[str]] = frozenset({"add", "modify", "delete"})

_ALNUM: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


@dataclass(frozen=True)
class PatchPolicyEvaluation:
    """The pure verdict of the patch policy scan.

    ``conflict`` is True when any rule in the pipeline hit; ``rule`` names the
    winning stage (the first in ``RULE_ORDER``) and ``path`` the offending
    file. On a clean patch all three are unset. The verdict never carries
    matched content or fingerprint values.
    """

    conflict: bool
    rule: str | None = None
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
    for allowed in allowed_paths:
        _validate_str(allowed, field="allowed path")
        _validate_int(_component_count(allowed), field="allowed path components")
        if allowed.startswith("/") or allowed.endswith("/"):
            raise _invalid("allowed path must be repo-relative without edge slashes")

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
        path = _validate_str(entry.get("path"), field="file path")
        if entry.get("change_type") not in CHANGE_TYPES:
            raise _invalid("unknown change type")
        _validate_int(entry.get("size_bytes"), field="file size")
        if not isinstance(entry.get("is_binary"), bool):
            raise _invalid("is_binary must be a boolean")
        content = entry.get("content")
        if not isinstance(content, str):
            raise _invalid("content must be a string")
        if entry["change_type"] == "delete" and content:
            raise _invalid("a delete entry carries no content")
        if path in seen:
            raise _invalid("duplicate file path in patch")
        seen.add(path)


def _component_count(path: str) -> int:
    return len([component for component in path.split("/") if component])


def _is_within(path: str, prefix: str) -> bool:
    """Component-boundary containment: ``src/dal`` covers ``src/dal/x.py``
    and ``src/dal`` itself, but never ``src/dalfoo``. A path carrying a
    ``..`` component can stringually start with the allowed prefix while
    escaping it (``src/app/../../escape.py``), so traversal components are
    never inside — matching ``path.py``'s escape rule."""
    if any(component == ".." for component in path.split("/")):
        return False
    return path == prefix or path.startswith(prefix + "/")


def _hits_protected_path(path: str) -> bool:
    if any(path.startswith(prefix) for prefix in PROTECTED_PATH_PREFIXES):
        return True
    basename = path.rsplit("/", 1)[-1]
    return basename == ".env" or basename.startswith(".env.")


def _hits_structural_secret(content: str) -> bool:
    for marker in PEM_MARKERS:
        if marker in content:
            return True
    for prefix, min_tail in KEY_PREFIXES:
        start = 0
        while True:
            index = content.find(prefix, start)
            if index < 0:
                break
            before_ok = index == 0 or content[index - 1] not in _ALNUM
            tail = 0
            cursor = index + len(prefix)
            while cursor < len(content) and content[cursor] in _ALNUM:
                tail += 1
                cursor += 1
            if before_ok and tail >= min_tail:
                return True
            start = index + 1
    return False


def _first_hit(
    rule: str,
    files: list[dict[str, Any]],
    allowed_paths: list[str],
    fingerprints: list[str],
) -> str | None:
    """The first file path hitting this pipeline stage, or None.

    One uniform signature for every stage — the evaluator calls it in
    ``RULE_ORDER`` without adapting arguments per rule (CLAUDE.md §5.2).
    """
    for entry in files:
        path = entry["path"]
        content = entry["content"]
        if rule == "leak_fingerprint":
            # Exact substring: the fingerprints are the orchestrator's
            # credential canary set for this run.
            if any(fp in content for fp in fingerprints):
                return path
        elif rule == "leak_structural":
            if _hits_structural_secret(content):
                return path
        elif rule == "protected_path":
            if _hits_protected_path(path):
                return path
        elif rule == "out_of_bounds":
            if not any(_is_within(path, prefix) for prefix in allowed_paths):
                return path
        elif rule == "binary":
            if entry["change_type"] != "delete" and entry["is_binary"]:
                return path
        elif rule == "size":
            if entry["change_type"] != "delete" and (
                entry["size_bytes"] > MAX_PATCH_FILE_SIZE_BYTES
            ):
                return path
    return None


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
        hit = _first_hit(
            rule,
            facts["files"],
            facts["allowed_paths"],
            facts["secret_fingerprints"],
        )
        if hit is not None:
            return PatchPolicyEvaluation(conflict=True, rule=rule, path=hit)
    return PatchPolicyEvaluation(conflict=False)
