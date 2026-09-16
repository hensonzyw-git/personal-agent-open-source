"""US / AI systemic-risk monitor — self-contained subsystem.

A single-user, replayable monitor of the AI capex / credit / market feedback
loop. It produces four 0-100 risk scores (MBS, CSS, RCS, AFRS), a four-stage state,
and explainable alerts from versioned rules in ``spec/scoring_policy.yml``.

This package is deliberately separate from Personal Agent's Agent runtime; the
only coupling is the Phase 5 daily-report bridge into the APNs push path.
"""

__version__ = "0.1.0"
