"""`DEV-034`: the closed vocabulary. Everything measurable is named here.

This file is the whole of `secret scan 为零`. `metrics.MetricRegistry` refuses
any label value not listed below, so reviewing this file is reviewing every
string that can ever reach a metric line in the journal. It should be read that
way: not "are these the metrics we want" but "is every one of these strings safe
to print".

Design 10.4 lists what to measure. Two of its items are deliberately **not**
here, because they are facts to be read rather than events to be counted:

- tool state distribution (`commit_unknown`, `verification_pending`, ...) and
- SQLite WAL size, disk free, backup age, last restore drill.

Counting those in memory would make them lie after a restart, exactly when
someone is most likely to be looking. They are derived from the database and
the filesystem at report time instead.

Two more are declared but **not yet emitted**, and their gaps are named rather
than left to be discovered:

- `push_send_total` — the APNs inputs landed on 2026-08-01 but no push code
  exists yet (`DEV-028`'s second half). The metric is declared so wiring it is
  a one-line call rather than a catalog change under time pressure.
- backup age has no metric at all because `DEV-035` has no backups to age.

`route` is deliberately a small group rather than one value per path. Per-path
labels drift the moment a route is added, and a label whose values track a URL
space is one refactor away from carrying an id. The groups mirror the Nginx
rate-limit zones in `deploy/nginx/agent.example.invalid.conf`, so a number here can
be compared against a 429 there without a translation step.
"""

from __future__ import annotations

from typing import Final, Mapping

from personal_agent_core.metrics import MetricSpec

#: Mirrors the Nginx `limit_req` zones, plus `other` so an unrouted request is
#: still countable without inventing a label.
ROUTE_GROUPS: Final[frozenset[str]] = frozenset(
    {"enrollment", "auth", "chat", "operations", "review", "capabilities", "other"}
)

#: Coarse on purpose. A status *code* is fine to print, but the moment the set
#: is open someone passes the provider's message with it.
STATUS_CLASSES: Final[frozenset[str]] = frozenset(
    {"2xx", "3xx", "4xx", "429", "5xx", "transport_error", "timeout"}
)

OUTCOMES: Final[frozenset[str]] = frozenset({"ok", "error"})


def _spec(
    name: str, kind: str, unit: str, help: str, labels: Mapping[str, frozenset[str]]
) -> tuple[str, MetricSpec]:
    return name, MetricSpec(
        name=name, kind=kind, unit=unit, help=help, labels=labels  # type: ignore[arg-type]
    )


CATALOG: Final[Mapping[str, MetricSpec]] = dict(
    [
        # --- request path -----------------------------------------------------
        _spec(
            "api_request_seconds",
            "latency",
            "seconds",
            "Time to serve one Client API request, by route group.",
            {"route": ROUTE_GROUPS, "outcome": OUTCOMES},
        ),
        _spec(
            "model_turn_seconds",
            "latency",
            "seconds",
            "One model turn, provider-side latency included.",
            {"provider": frozenset({"glm"}), "outcome": OUTCOMES},
        ),
        _spec(
            "mcp_call_seconds",
            "latency",
            "seconds",
            "One MCP operation. `initialize` is separate so a legacy-transport "
            "handshake cost cannot hide inside call latency (design 10.4).",
            {
                "operation": frozenset({"initialize", "discovery", "list", "call"}),
                "transport": frozenset({"v2", "legacy"}),
                "outcome": OUTCOMES,
            },
        ),
        # --- refusals ---------------------------------------------------------
        _spec(
            "policy_denied_total",
            "counter",
            "denials",
            "Tool intents the policy refused, by the reason it refused them.",
            {
                "reason": frozenset(
                    {
                        "not_allowlisted",
                        "not_discovered",
                        "schema_drift",
                        "scope_not_granted",
                        "risk_not_permitted",
                    }
                )
            },
        ),
        _spec(
            "schema_mismatch_total",
            "counter",
            "mismatches",
            "Ledger schema drift observed at validation time.",
            {"table_kind": frozenset({"expense", "income", "family_fund"})},
        ),
        # --- providers --------------------------------------------------------
        _spec(
            "provider_response_total",
            "counter",
            "responses",
            "Outbound provider responses by status class. Never a body.",
            {
                "provider": frozenset({"feishu", "frankfurter", "glm", "apns"}),
                "status_class": STATUS_CLASSES,
            },
        ),
        # --- writes -----------------------------------------------------------
        _spec(
            "write_outcome_total",
            "counter",
            "writes",
            "Terminal outcome of a governed write. `commit_unknown` and "
            "`mismatch` are the two design 10.4 alerts on immediately.",
            {
                "tool": frozenset(
                    {
                        "finance.log_expense",
                        "finance.log_income",
                        "finance.update_family_fund",
                    }
                ),
                "outcome": frozenset(
                    {
                        "succeeded",
                        "commit_unknown",
                        "mismatch",
                        "needs_manual_review",
                        "failed_safe",
                    }
                ),
            },
        ),
        _spec(
            "audit_write_failed_total",
            "counter",
            "failures",
            "Audit events that could not be persisted. Any value above zero is "
            "an immediate alert: the write that carried it failed closed, and "
            "the trail has a hole a chain check cannot see.",
            {},
        ),
        # --- review -----------------------------------------------------------
        _spec(
            "review_build_seconds",
            "latency",
            "seconds",
            "Time to assemble one daily review.",
            {"outcome": OUTCOMES},
        ),
        _spec(
            "review_decision_total",
            "counter",
            "decisions",
            "What Henson did with a review card.",
            {"decision": frozenset({"ack", "defer"})},
        ),
        # --- declared, not yet emitted (DEV-028's push half) ------------------
        _spec(
            "push_send_total",
            "counter",
            "sends",
            "APNs send outcomes. NOT YET EMITTED: no push code exists, so this "
            "reading zero means 'nothing tried', not 'nothing failed'.",
            {"outcome": frozenset({"accepted", "rejected", "unavailable"})},
        ),
    ]
)

#: Metrics that exist in the catalog but nothing calls yet. Named explicitly so
#: a reporter can say "not wired" instead of showing a zero that reads as health.
NOT_YET_EMITTED: Final[frozenset[str]] = frozenset({"push_send_total"})
