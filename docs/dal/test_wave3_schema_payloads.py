#!/usr/bin/env python3
"""Positive/negative payload vectors for the Wave 3 (DAL-021..024) closed schemas.

The eight schemas in ``docs/dal/manifests/*_schema_v1.0.json`` encode the frozen
field sets, enums and cross-constraints of ``DAL021-024_合同冻结包_v0.1.md`` §3-§6.
This script pins the behaviours that a hand edit or generator drift could silently
loosen. Each vector is a (schema, payload, expect_valid) triple; ``expect_valid``
is asserted, so a regression in either direction (tightening or loosening) fails.

It also pins the three review findings from the first Wave 3 meta-review round so
they stay closed:
  * F2 — ``$defs.codex_config.proxy`` MUST be locked to ``null`` (§3.1「config.proxy
    MUST 恒为 null」), never ``oneOf[null, {scheme,host,port}]``.
  * F3 — a ``post_fix_verification`` request with ``open_findings_sha256 = null``
    MUST be rejected (the frozen three-state rule requires a 64-hex digest there).
  * F4 — §4「去除首尾空白后仍非空」 MUST reject whitespace-only plan text items
    (``" "``, ``"\\t"``), which ``minLength: 1`` alone would accept.

This is documentation validation, not the review gate: passing does not claim the
Wave 3 independent review gate has run.
"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "manifests"

S64 = "a" * 64
S40 = "b" * 40


def _schema(stem: str) -> dict:
    return json.loads((OUT / f"{stem}_schema_v1.0.json").read_text(encoding="utf-8"))


def _check(name: str, payload: dict, expect_valid: bool, failures: list[str]) -> None:
    schema = _schema(name)
    errs = list(Draft202012Validator(schema).iter_errors(payload))
    ok = not errs
    if ok != expect_valid:
        detail = "; ".join(e.message for e in errs[:3])
        failures.append(f"{name}: expected_valid={expect_valid} actual={ok} :: {detail}")


def _base_request() -> dict:
    return {
        "schema_version": "dal.codex-adapter-request/1.0",
        "task_id": "t1", "feature_id": "f1", "run_id": "r1",
        "operation_kind": "planning", "model": "gpt-5.6-sol",
        "harness": "codex", "harness_version": "0.147.0",
        "binary_sha256": S64, "endpoint_policy_sha256": S64,
        "output_schema": "dal.plan-artifact/1.0",
        "cwd": "/tmp/wt", "base_sha": S40,
        "input_manifest_ref": "ref://m", "input_manifest_sha256": S64,
        "allowed_paths": [{"path": "/repo/a.md", "access": "read"}],
        "context_binding": {
            "source_type": "planning-controller", "agent_identity": "a1",
            "context_sha256": S64, "config_sha256": S64, "prompt_template_sha256": S64,
            "open_findings_sha256": None,
        },
        "timeout_wall_seconds": 300, "max_turns": 1, "max_tool_calls": 0,
        "redaction_policy": {"canonicalizer": "rfc8785-jcs/1.0", "rules_version": "v1"},
    }


def _mk_item(role: str, media: str, art: str | None) -> dict:
    return {"role": role, "artifact_schema_version": art, "media_type": media,
            "protected_ref": "ref://x", "sha256": S64, "size_bytes": 0}


def _base_response() -> dict:
    return {
        "schema_version": "dal.codex-adapter-response/1.0",
        "task_id": "t1", "model": "m", "harness": "h", "harness_version": "v",
        "binary_sha256": S64, "endpoint_policy_sha256": S64,
        "result_status": "succeeded",
        "requested_output_schema": "dal.plan-artifact/1.0",
        "validated_output_schema": "dal.plan-artifact/1.0",
        "input_manifest_sha256": S64, "context_binding_sha256": S64,
        "final_payload_ref": "ref://p", "final_payload_sha256": S64,
        "exit_code": 0,
        "started_at": "2026-08-17T00:00:00Z", "ended_at": "2026-08-17T00:00:01Z",
        "usage": {"provider_reported": False, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "failure_class": None, "reason_code": None,
        "raw_evidence_ref": "ref://e", "raw_evidence_sha256": S64,
        "redaction_scan": "passed", "endpoint_policy": "passed", "quarantined": False,
    }


def _plan(scope: list, title: str = "t", desc: str = "d") -> dict:
    return {
        "schema_version": "dal.plan-artifact/1.0", "feature_id": "f1", "base_sha": S40,
        "prd": {"scope": scope, "non_goals": [], "risks": []},
        "technical_design": {"change_points": ["c"], "boundaries": ["b"], "rollback_steps": ["not_applicable: none"]},
        "tasks": [{"task_id": "t1", "order": 1, "title": title,
                   "allowed_paths": [{"path": "src/a.py", "path_type": "file"}],
                   "acceptance_ids": ["a1"], "dependency_task_ids": []}],
        "acceptance_criteria": [{"acceptance_id": "a1", "description": desc, "verification_ids": ["v1"]}],
    }


def main() -> None:
    failures: list[str] = []

    # ---- request ----
    _check("codex-adapter-request", _base_request(), True, failures)
    r = _base_request(); r["output_schema"] = "dal.review-findings/1.0"  # op<->output mismatch
    _check("codex-adapter-request", r, False, failures)
    r = _base_request(); r["context_binding"]["source_type"] = "review-controller"  # source<->op mismatch
    _check("codex-adapter-request", r, False, failures)
    r = _base_request(); r["context_binding"]["open_findings_sha256"] = S64  # planning must be null
    _check("codex-adapter-request", r, False, failures)
    r = _base_request(); r["extra"] = 1  # unknown field
    _check("codex-adapter-request", r, False, failures)
    r = _base_request(); r["allowed_paths"] = [{"path": "/x", "access": "write"}]  # access fixed read
    _check("codex-adapter-request", r, False, failures)
    r = _base_request()  # F3: post_fix + null open_findings MUST reject
    r["operation_kind"] = "post_fix_verification"; r["output_schema"] = "dal.post-fix-verdict/1.0"
    r["context_binding"]["source_type"] = "review-controller"
    _check("codex-adapter-request", r, False, failures)
    r = _base_request()  # post_fix + 64-hex open_findings accepted
    r["operation_kind"] = "post_fix_verification"; r["output_schema"] = "dal.post-fix-verdict/1.0"
    r["context_binding"]["source_type"] = "review-controller"; r["context_binding"]["open_findings_sha256"] = S64
    _check("codex-adapter-request", r, True, failures)
    # F2: $defs.codex_config.proxy must be type:null only
    req_schema = _schema("codex-adapter-request")
    proxy = req_schema["$defs"]["codex_config"]["properties"]["proxy"]
    if proxy != {"type": "null"}:
        failures.append(f"codex-adapter-request: $defs.codex_config.proxy not locked to null: {proxy!r}")

    # ---- input-manifest ----
    def mf(kind: str, items: list[dict], result: str | None = None) -> dict:
        return {"schema_version": "dal.codex-input-manifest/1.0", "operation_kind": kind,
                "task_id": "t1", "feature_id": "f1", "run_id": "r1",
                "base_sha": S40, "result_sha": result, "items": items}
    plan_items = [_mk_item("requirement_artifact", "text/markdown", None), _mk_item("repo_rules", "text/markdown", None)]
    _check("codex-input-manifest", mf("planning", plan_items), True, failures)
    _check("codex-input-manifest", mf("planning", list(reversed(plan_items))), False, failures)  # order
    _check("codex-input-manifest", mf("planning", plan_items, result=S40), False, failures)  # planning result must be null
    bad_media = [_mk_item("requirement_artifact", "application/json", None), _mk_item("repo_rules", "text/markdown", None)]
    _check("codex-input-manifest", mf("planning", bad_media), False, failures)

    # ---- response ----
    _check("codex-adapter-response", _base_response(), True, failures)
    r = _base_response(); r["failure_class"] = "transient"; r["reason_code"] = "TRANSIENT_RETRY_EXHAUSTED"
    _check("codex-adapter-response", r, False, failures)  # succeeded + non-null failure
    r = _base_response(); r["final_payload_ref"] = None
    _check("codex-adapter-response", r, False, failures)  # succeeded + null payload
    r = _base_response(); r["result_status"] = "blocked"
    r["validated_output_schema"] = None; r["final_payload_ref"] = None; r["final_payload_sha256"] = None
    r["failure_class"] = "usage_limit"; r["reason_code"] = "POLICY_FAILURE"  # wrong pair
    _check("codex-adapter-response", r, False, failures)
    r = _base_response(); r["result_status"] = "cancelled"
    r["validated_output_schema"] = None; r["final_payload_ref"] = None; r["final_payload_sha256"] = None
    r["failure_class"] = None; r["reason_code"] = None; r["exit_code"] = None
    _check("codex-adapter-response", r, True, failures)  # cancelled all-null
    r = _base_response(); r["result_status"] = "failed"
    r["validated_output_schema"] = None; r["final_payload_ref"] = None; r["final_payload_sha256"] = None
    r["failure_class"] = "task_failure"; r["reason_code"] = "TEST_BLOCKED"  # task_failure not legal
    _check("codex-adapter-response", r, False, failures)

    # ---- redacted-log ----
    rl = {"schema_version": "dal.codex-redacted-log/1.0", "task_id": "t1", "run_id": "r1",
          "started_at": "2026-08-17T00:00:00Z", "ended_at": "2026-08-17T00:00:01Z", "exit_code": 0,
          "usage": {"provider_reported": False, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
          "failure_class": None, "redaction_scan": "passed", "endpoint_policy": "passed"}
    _check("codex-redacted-log", rl, True, failures)
    rl2 = dict(rl); rl2["prompt"] = "leak"
    _check("codex-redacted-log", rl2, False, failures)

    # ---- plan-artifact (F4: trimmed-non-empty) ----
    _check("plan-artifact", _plan(["s1"]), True, failures)
    _check("plan-artifact", _plan([" "]), False, failures)          # F4 whitespace-only scope
    _check("plan-artifact", _plan(["  a  "]), True, failures)       # trimmed still non-empty
    _check("plan-artifact", _plan(["s"], title="\t"), False, failures)  # F4 whitespace title
    _check("plan-artifact", _plan(["s"], desc=" "), False, failures)    # F4 whitespace description
    _check("plan-artifact", _plan([]), False, failures)             # scope minItems 1

    # ---- round-2 FINDING 1: repo-relative POSIX path format is schema-locked ----
    def _plan_path(p: str) -> dict:
        d = _plan(["s"]); d["tasks"][0]["allowed_paths"][0]["path"] = p
        return d
    _check("plan-artifact", _plan_path("../../shared"), False, failures)  # `..` segment
    _check("plan-artifact", _plan_path("/etc"), False, failures)          # absolute path
    _check("plan-artifact", _plan_path(".git/config"), False, failures)   # .git subtree
    _check("plan-artifact", _plan_path("a/../b"), False, failures)        # mid-path `..`
    _check("plan-artifact", _plan_path(".gitignore"), True, failures)     # dotfile ≠ .git subtree
    _check("plan-artifact", _plan_path(".github/ci.yml"), True, failures) # .github ≠ .git subtree
    _check("plan-artifact", _plan_path("src/auth.py"), True, failures)    # ordinary rel path

    # ---- round-2 FINDING 2: first task order==1 is schema-locked ----
    p = _plan(["s"]); p["tasks"][0]["order"] = 7
    _check("plan-artifact", p, False, failures)                     # order must start at 1

    # ---- review-findings ----
    def finding(fid: str = "F1") -> dict:
        return {"finding_id": fid, "severity": "P1",
                "location": {"path": "src/a.py", "line_start": 1, "line_end": 2, "anchor_sha": S40},
                "summary": "s", "failure_scenario": "fs", "category": "correctness"}
    def rf(findings: list, disposition: str) -> dict:
        return {"schema_version": "dal.review-findings/1.0", "review_id": "rv1",
                "reviewed_input_manifest_sha256": S64, "reviewed_diff_sha256": S64,
                "base_sha": S40, "result_sha": S40, "findings": findings, "acceptance_gaps": [],
                "coverage": [{"acceptance_id": "a1", "verification_ids": ["v1"]}], "disposition": disposition}
    _check("review-findings", rf([], "approve"), True, failures)
    _check("review-findings", rf([finding()], "request_changes"), True, failures)
    _check("review-findings", rf([finding()], "approve"), False, failures)  # findings + approve
    # round-2 FINDING 1: finding location.path uses the same repo-relative policy
    r = rf([finding()], "request_changes"); r["findings"][0]["location"]["path"] = "../../x"
    _check("review-findings", r, False, failures)                   # location path `..` segment

    # ---- session-binding ----
    sb = {"schema_version": "dal.reviewer-session-binding/1.0", "session_id": "thread_1",
          "independence_key": S64, "binding_key_id": "bk1", "context_binding_sha256": S64}
    _check("reviewer-session-binding", sb, True, failures)
    sb2 = dict(sb); sb2["independence_key"] = "nothex"
    _check("reviewer-session-binding", sb2, False, failures)

    # ---- post-fix-verdict ----
    def res(item_id: str, fid: str = "F1", status: str = "closed") -> dict:
        return {item_id: fid, "status": status, "summary": "s", "evidence_sha256": [S64]}
    def pfv(**over) -> dict:
        base = {"schema_version": "dal.post-fix-verdict/1.0", "verdict_id": "v1",
                "reviewed_input_manifest_sha256": S64, "original_review_sha256": S64, "fix_diff_sha256": S64,
                "base_sha": S40, "result_sha": S40,
                "finding_resolutions": [res("finding_id")], "acceptance_gap_resolutions": [],
                "new_findings": [], "acceptance_verified": True, "verdict": "verified"}
        base.update(over)
        return base
    _check("post-fix-verdict", pfv(), True, failures)
    _check("post-fix-verdict", pfv(new_findings=[finding()], verdict="changes_requested"), True, failures)
    _check("post-fix-verdict", pfv(new_findings=[finding()]), False, failures)  # new_findings + verified
    _check("post-fix-verdict", pfv(finding_resolutions=[res("finding_id", status="remaining")]), False, failures)
    _check("post-fix-verdict", pfv(acceptance_verified=False), False, failures)

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        raise SystemExit(1)
    print("wave3 schema payload vectors: PASS")


if __name__ == "__main__":
    main()
