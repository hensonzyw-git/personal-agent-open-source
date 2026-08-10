"""The deterministic state machine (DAL-009).

The machine is an interpreter over the frozen TransitionSpec registry, not a
hand-written transition table. Contract §2.3.1 makes the registry an exhaustive
allowlist; an interpreter cannot drift from it, whereas a hand-written table
only agrees with it until someone edits one of the two.
"""
