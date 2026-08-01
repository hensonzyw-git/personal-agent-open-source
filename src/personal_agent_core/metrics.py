"""`DEV-034`: the metric registry, and why it refuses more than it records.

Technical design 10.4 lists what to measure and sets one acceptance bar for it:
`secret scan 为零`. There are two ways to meet that bar and they are not equally
strong.

The weak one is to record whatever the call site has and scrub it on the way
out. `feishu.redaction.redact_for_log` does exactly that and is right for its
job -- it is a backstop over diagnostic prose nobody can enumerate in advance.
But scrubbing is pattern matching, so it is only ever as good as the last
pattern someone remembered, and a metric label is a place where a caller will
eventually pass `error=str(exc)` or `table=table_id` because it was convenient.
By then the leak is in the journal.

The strong one, and the one implemented here: **a label value that was not
declared in advance cannot be recorded at all.** Every metric names its labels,
every label names the exact set of values it may take, and anything else raises.
No free-form string reaches a metric, so there is no scanning step to get right
and no pattern to keep current. `secret scan 为零` becomes a property of the
type, not a result of a scan.

The cost is real and deliberate: adding a metric or a new label value means
editing the catalog. That is the point. The moment a caller can invent a label
value, this file's guarantee is gone.

Three further decisions worth stating because each could be read as a bug:

- **Latency percentiles are over a bounded window, not all time.** A metric
  process that grows without limit is a leak; a fixed set of buckets loses the
  ability to answer "P95 of what actually happened". So each series keeps the
  last `window` observations exactly and the snapshot says how many it holds.
  A P95 over 40 samples is a P95 over 40 samples, and it is labelled as such.
- **Observed counters reset when the process restarts.** They are in memory on
  purpose: the alternative is writing to the same SQLite the service is trying
  to report on, which turns an observability failure into a write failure.
  Anything that must survive a restart -- the tool state distribution, WAL size,
  backup age -- is *derived* from the database when asked, not counted here.
- **This module never decides what is wrong.** It records and reports. Alert
  thresholds live with the alert evaluator, so a threshold change cannot
  accidentally change what was measured.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping

MetricKind = Literal["counter", "latency"]

#: How many observations one latency series keeps. Small on purpose: this is a
#: single-user system, and a window that spans days would answer a question
#: nobody asked while pinning memory for every label combination.
DEFAULT_WINDOW: Final[int] = 512


class MetricError(RuntimeError):
    """A metric was used in a way the catalog does not allow.

    Deliberately not a subclass of anything the request path catches. A metric
    misuse is a programming error to fix, not a runtime condition to degrade
    around -- and failing loudly in a test is the whole mechanism by which an
    undeclared label value never reaches production.
    """


@dataclass(frozen=True)
class MetricSpec:
    """One metric, and the complete set of label values it may ever carry."""

    name: str
    kind: MetricKind
    unit: str
    help: str
    #: label name -> the closed set of values that label may take. A metric with
    #: no labels declares an empty mapping, and then no labels may be passed.
    labels: Mapping[str, frozenset[str]]

    def validate_labels(self, given: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        """Return the label pairs in a stable order, or raise.

        Every failure mode is a raise rather than a repair: dropping an unknown
        label would silently merge two different series, and coercing a value
        would record something that never happened.
        """
        missing = set(self.labels) - set(given)
        if missing:
            raise MetricError(
                f"{self.name} requires labels {sorted(self.labels)}; "
                f"missing {sorted(missing)}"
            )
        unexpected = set(given) - set(self.labels)
        if unexpected:
            raise MetricError(
                f"{self.name} does not declare labels {sorted(unexpected)}"
            )
        pairs: list[tuple[str, str]] = []
        for label in sorted(self.labels):
            value = given[label]
            if not isinstance(value, str):
                raise MetricError(
                    f"{self.name}.{label} must be a string, got {type(value).__name__}"
                )
            allowed = self.labels[label]
            if value not in allowed:
                # The message names the label and the allowed set but *never*
                # the rejected value: the whole reason a value gets rejected is
                # that it might be a secret, and an exception message travels to
                # the same journal a metric would have.
                raise MetricError(
                    f"{self.name}.{label} accepts only {sorted(allowed)}; "
                    "the supplied value is not one of them and is not repeated "
                    "here in case it is sensitive"
                )
            pairs.append((label, value))
        return tuple(pairs)


class MetricRegistry:
    """Records observations against a fixed catalog. Thread-safe."""

    def __init__(
        self, specs: Mapping[str, MetricSpec], *, window: int = DEFAULT_WINDOW
    ) -> None:
        if window < 1:
            raise MetricError("window must be at least 1")
        for name, spec in specs.items():
            if name != spec.name:
                raise MetricError(
                    f"catalog key {name!r} does not match spec name {spec.name!r}"
                )
        self._specs = dict(specs)
        self._window = window
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._latencies: dict[
            tuple[str, tuple[tuple[str, str], ...]], deque[float]
        ] = {}

    def _spec(self, name: str, kind: MetricKind) -> MetricSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise MetricError(f"{name!r} is not in the metric catalog")
        if spec.kind != kind:
            raise MetricError(
                f"{name} is a {spec.kind}, not a {kind}"
            )
        return spec

    def increment(self, name: str, *, amount: int = 1, **labels: str) -> None:
        spec = self._spec(name, "counter")
        if amount < 0:
            # A counter that can go down is not a counter, and a negative rate
            # would be read as a wrap-around by anything downstream.
            raise MetricError(f"{name} is monotonic; amount must not be negative")
        key = (name, spec.validate_labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def observe(self, name: str, seconds: float, **labels: str) -> None:
        spec = self._spec(name, "latency")
        if seconds < 0:
            raise MetricError(f"{name} cannot observe a negative duration")
        key = (name, spec.validate_labels(labels))
        with self._lock:
            series = self._latencies.get(key)
            if series is None:
                series = deque(maxlen=self._window)
                self._latencies[key] = series
            series.append(float(seconds))

    def snapshot(self) -> list[dict[str, Any]]:
        """Every series, in a stable order, carrying only declared strings.

        The return value is plain data so the caller decides the format. Nothing
        in it can carry a secret, because nothing in it came from anywhere but
        the catalog and arithmetic over floats.
        """
        with self._lock:
            counters = dict(self._counters)
            latencies = {key: list(value) for key, value in self._latencies.items()}

        rows: list[dict[str, Any]] = []
        for (name, pairs), value in counters.items():
            rows.append(
                {
                    "metric": name,
                    "kind": "counter",
                    "unit": self._specs[name].unit,
                    "labels": dict(pairs),
                    "value": value,
                }
            )
        for (name, pairs), samples in latencies.items():
            rows.append(
                {
                    "metric": name,
                    "kind": "latency",
                    "unit": self._specs[name].unit,
                    "labels": dict(pairs),
                    # `count` is not decoration: a P95 is only meaningful next to
                    # how many observations it summarises, and this window is
                    # deliberately small.
                    "count": len(samples),
                    "p50": percentile(samples, 50),
                    "p95": percentile(samples, 95),
                    "max": max(samples) if samples else None,
                }
            )
        rows.sort(key=lambda row: (row["metric"], sorted(row["labels"].items())))
        return rows


def percentile(samples: list[float], nth: float) -> float | None:
    """Nearest-rank percentile over the retained window.

    Nearest-rank rather than interpolation because an interpolated P95 reports a
    latency that no request ever had. With a window this small that matters:
    "the 95th slowest thing we actually saw" is answerable and checkable against
    a log; an interpolated value is neither.
    """
    if not samples:
        return None
    if not 0 < nth <= 100:
        raise MetricError("percentile must be within (0, 100]")
    ordered = sorted(samples)
    # ceil(nth/100 * n) with integer arithmetic, then clamp into range.
    rank = -(-int(nth * len(ordered)) // 100)
    return ordered[max(0, min(rank, len(ordered)) - 1)]
