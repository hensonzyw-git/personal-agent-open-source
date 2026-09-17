#!/usr/bin/env python3
"""Read-only export of the host's configured routing as a sanitized freeze.

Produces a ``dal.routing-freeze/1.0`` manifest plus ``sha256(JCS(manifest))``
from the real Home Mac config. Hard guarantees:

* never reads the *contents* of a credential (only ``stat``-s the auth store),
* never writes and never touches the network,
* freezes only routing facts (model names, endpoints, CLI versions) in clear,
* reduces every credential to a ``sha256`` reference + DAL-006 compliance facts,
* drops anything it cannot classify and lists its path in ``redacted`` — the
  fail-closed witness. A future agent/provider/CCR shape can therefore never
  silently leak: at worst it shows up in ``redacted`` and is re-reviewed.

The classifier is provider-agnostic. Agent/provider/CCR names appear only as
manifest *values*; the rule logic keys off a generic role vocabulary. The digest
binds the frozen facts, never the freeze timestamp.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_HOME = Path.home()
_CODEX_HOME = _HOME / ".codex"
_CLAUDE_HOME = _HOME / ".claude"

# Secret-signalling substrings (case-insensitive). Broad on purpose: over-matching
# here only over-redacts, which is the safe direction.
_CREDENTIAL_SEGMENTS = ("key", "token", "secret", "password", "credential",
                        "bearer", "authorization")

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Live-updating fields that CCR/Claude Code rewrite per session — transient state,
# not a stable routing fact. Freezing them would make the digest churn between runs.
# The stable routing facts are the `env.*_MODEL` mapping and the CCR config file.
# (Observed 2026-08-22: ~/.claude/settings.json `model` changed between two reads.)
_TRANSIENT_TOP_LEVEL: dict[str, set[str]] = {
    str(_CLAUDE_HOME / "settings.json"): {"model"},
}


def _rel(path: str) -> str:
    return path.replace(str(_HOME), "~", 1)


def _strip_ansi(value: str) -> str:
    return _ANSI.sub("", value)


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cli_version(binary: str) -> str | None:
    exe = shutil.which(binary)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True,
                             text=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError):
        return None
    first = (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
    m = re.search(r"(\d+\.\d+\.\d+)", first)
    return m.group(1) if m else first


def _load_toml(path: Path) -> dict[str, Any]:
    import tomllib
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _flatten(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(node, dict):
        out: list[tuple[str, Any]] = []
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            out.extend(_flatten(value, path))
        return out
    return [(prefix, node)]


def _classify_key(key: str) -> str:
    """'credential' | 'routing' | 'usage' for a leaf key (case-insensitive)."""
    k = key.lower()
    if any(seg in k for seg in _CREDENTIAL_SEGMENTS):
        return "credential"
    if (k == "model" or k == "model_provider" or k.endswith("_model")
            or k.endswith("_url") or "base_url" in k
            or k in ("route", "router", "classifier", "endpoint")):
        return "routing"
    return "usage"


def _hostport(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return f"{parsed.hostname}:{port}"
    except ValueError:
        return None
    return None


def _stat_credential(path: Path) -> dict[str, Any]:
    st = path.stat()
    parent = path.parent.stat()
    return {
        "role": "auth-store",
        "ref": _sha256(_rel(str(path))),
        "mode": "file",
        "owner": st.st_uid,
        "perm": oct(st.st_mode & 0o777)[2:].zfill(4),
        "parent_perm": oct(parent.st_mode & 0o777)[2:].zfill(4),
    }


def _credential_store_violations() -> list[dict[str, str]]:
    """The hard gate: credential stores must be 0600 file / 0700 parent (DAL-006 §3.3).

    A plaintext key inside a 0600-protected third-party store (CCR's config.sqlite) is NOT a
    violation here. Henson ruled (2026-08-22, option 2) that the freeze's "no plaintext" gate
    applies only to credentials the DAL system itself writes; CCR-native storage is judged by
    its file permissions. Loose perms — or a store we cannot read to verify — is the hard gate.
    The pinned-host runtime check is deliberately NOT here — that is DAL-026's job.
    """
    violations: list[dict[str, str]] = []
    for path in (
        _HOME / ".claude-code-router" / "config.sqlite",
        _CODEX_HOME / "auth.json",
        _CLAUDE_HOME / "auth.json",
    ):
        if not path.is_file():
            continue
        try:
            st = path.stat()
            parent = path.parent.stat()
            if st.st_mode & 0o777 != 0o600:
                violations.append({
                    "code": "credential-store-loose-file-perm",
                    "store": _rel(str(path)),
                    "name": oct(st.st_mode & 0o777)[2:].zfill(4),
                })
            if parent.st_mode & 0o777 != 0o700:
                violations.append({
                    "code": "credential-store-loose-parent-perm",
                    "store": _rel(str(path.parent)),
                    "name": oct(parent.st_mode & 0o777)[2:].zfill(4),
                })
        except OSError:
            violations.append({
                "code": "credential-store-unreadable",
                "store": _rel(str(path)),
                "name": "",
            })
    return violations


def _plaintext_credential_names() -> list[dict[str, str]]:
    """Informational (not a hard gate): CCR-native plaintext-at-rest credentials.

    Detects, without ever outputting a key value, which credentials CCR stores unencrypted.
    Henson accepted these as CCR-native (2026-08-22, option 2): they are recorded here so the
    fact stays visible and drift-tracked, but they do not mark the freeze non-compliant.
    """
    names: list[dict[str, str]] = []
    ccr_db = _HOME / ".claude-code-router" / "config.sqlite"
    if not ccr_db.is_file():
        return names
    try:
        con = sqlite3.connect(f"file:{ccr_db}?mode=ro", uri=True)
        for name, enc in con.execute("SELECT name, encryption FROM api_keys"):
            if enc == "plain":
                names.append({"name": name or "", "store": "api_keys", "encryption": "plain"})
        con.close()
    except sqlite3.Error:
        names.append({"name": "", "store": "api_keys", "encryption": "unreadable"})
    try:
        con = sqlite3.connect(f"file:{ccr_db}?mode=ro", uri=True)
        row = con.execute("SELECT value_json FROM app_config WHERE key='default'").fetchone()
        con.close()
        if row:
            data = json.loads(row[0])
            for p in data.get("Providers", []) or []:
                if isinstance(p, dict) and isinstance(p.get("api_key"), str) and p["api_key"]:
                    names.append({
                        "name": p.get("name") or p.get("id") or "",
                        "store": "app_config.Providers[].api_key",
                        "encryption": "plain",
                    })
    except (sqlite3.Error, json.JSONDecodeError):
        names.append({"name": "", "store": "app_config", "encryption": "unreadable"})
    return names


def main() -> int:
    models: set[str] = set()
    endpoints: set[str] = set()
    routing: list[dict[str, str]] = []
    credentials: list[dict[str, Any]] = []
    redacted: list[str] = []
    violations: list[dict[str, str]] = _credential_store_violations()
    plaintext_credentials: list[dict[str, str]] = _plaintext_credential_names()
    dirty_models: list[str] = []
    transient: list[str] = []

    cli = []
    for agent, binary in (("codex", "codex"), ("claude-code", "claude")):
        version = _cli_version(binary)
        if version:
            cli.append({"agent": agent, "cli": binary, "version": version})

    for home in (_CODEX_HOME, _CLAUDE_HOME):
        auth = home / "auth.json"
        if auth.is_file():
            credentials.append(_stat_credential(auth))

    ccr_db = _HOME / ".claude-code-router" / "config.sqlite"
    if ccr_db.is_file():
        credentials.append(_stat_credential(ccr_db))

    sources: list[tuple[Path, dict[str, Any]]] = []
    for path in (
        _CODEX_HOME / "config.toml",
        _CODEX_HOME / "claude-code-router.config.toml",
        _CODEX_HOME / "ccr-model-catalog.json",
        _CLAUDE_HOME / "settings.json",
    ):
        if not path.is_file():
            redacted.append(f"{_rel(str(path))} (missing)")
            continue
        try:
            body = _load_toml(path) if path.suffix == ".toml" else _load_json(path)
        except Exception as exc:  # noqa: BLE001
            redacted.append(f"{_rel(str(path))} (unparseable: {type(exc).__name__})")
            continue
        sources.append((path, body))

    for path, body in sources:
        for leaf_path, value in _flatten(body):
            key = leaf_path.split(".")[-1]
            if (str(path) in _TRANSIENT_TOP_LEVEL and "." not in leaf_path
                    and key in _TRANSIENT_TOP_LEVEL[str(path)]):
                transient.append(f"{_rel(str(path))}::{leaf_path}")
                continue
            kind = _classify_key(key)
            if kind == "credential":
                if isinstance(value, str):
                    credentials.append({
                        "role": "key-helper" if "key" in key.lower() and "helper" in key.lower()
                        else "secret-field",
                        "ref": _sha256(value),
                    })
                else:
                    credentials.append({
                        "role": "secret-field",
                        "ref": _sha256(json.dumps(value, sort_keys=True)),
                    })
            elif kind == "routing":
                if key.lower() == "model_provider":
                    # router name, not a model — captured in routing[], not providers[]
                    if isinstance(value, str):
                        routing.append({"slot": "router", "provider": value, "model": ""})
                    continue
                if isinstance(value, str):
                    cleaned = _strip_ansi(value)
                    if re.search(r"[0-9a-f]{16,}", cleaned):
                        dirty_models.append(f"{_rel(str(path))}::{leaf_path}")
                    if key.lower().endswith("_url") or "base_url" in key.lower():
                        hostport = _hostport(cleaned)
                        if hostport:
                            endpoints.add(hostport)
                    else:
                        models.add(cleaned)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            models.add(_strip_ansi(item))
                elif isinstance(value, dict):
                    for sub in ("id", "slug", "model"):
                        if isinstance(value.get(sub), str):
                            models.add(_strip_ansi(value[sub]))
            else:
                redacted.append(f"{_rel(str(path))}::{_rel(leaf_path)}")

    def _vendor(model: str) -> str:
        return model.split("/")[0].split("-")[0].split(".")[0] or "unknown"

    grouped: dict[str, dict[str, Any]] = {}
    for model in sorted(models):
        grouped.setdefault(_vendor(model), {"name": _vendor(model),
                                            "models": [], "endpoints": []})
        grouped[_vendor(model)]["models"].append(model)
    for endpoint in sorted(endpoints):
        grouped.setdefault("ccr-proxy", {"name": "ccr-proxy",
                                         "models": [], "endpoints": []})
        grouped["ccr-proxy"]["endpoints"].append(endpoint)

    manifest: dict[str, Any] = {
        "schema": "dal.routing-freeze/1.0",
        "host": "home-mac",
        "cli": cli,
        "providers": sorted(grouped.values(), key=lambda r: r["name"]),
        "routing": routing,
        "credentials": credentials,
        "redacted": sorted(redacted),
        "violations": violations,
        "plaintext_credentials": plaintext_credentials,
    }

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "docs" / "dal"))
    from dal_jcs import canonical_bytes
    digest = "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()

    manifest["frozen_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\n# digest {digest}", file=sys.stderr)
    print(f"# models: {len(models)} · endpoints: {len(endpoints)} · "
          f"routing slots: {len(routing)} · credentials: {len(credentials)} · "
          f"redacted: {len(redacted)} · dirty model values: {len(dirty_models)}",
          file=sys.stderr)
    for d in dirty_models:
        print(f"# dirty model value (hex/ansi): {d}", file=sys.stderr)
    for t in transient:
        print(f"# transient field excluded from freeze: {t}", file=sys.stderr)
    if violations:
        print("#", file=sys.stderr)
        print("# COMPLIANCE: VIOLATION — freeze is non-compliant.", file=sys.stderr)
        for v in violations:
            print(f"#   {v['code']}: {v['store']}"
                  + (f" perm={v['name']}" if v.get("name") else ""), file=sys.stderr)
        print("# Resolve before any downstream preflight/adapter consumes this freeze.",
              file=sys.stderr)
    else:
        print("# COMPLIANCE: OK — all credential stores 0600/0700.", file=sys.stderr)
    for p in plaintext_credentials:
        print(f"# plaintext (accepted, CCR-native): {p['name']} [{p['store']}]", file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
