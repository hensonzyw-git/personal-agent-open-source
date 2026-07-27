"""CAP-001 Session lifecycle and context assembly.

The modules here own the Timeline/Session split, the context budget, the
Compactor and the Context Builder. They live outside any model SDK on purpose
(`CLAUDE.md` §5, cross-cutting design §1.5): Google ADK may consume a built
context, but it does not own this system's Session, Memory, policy or deletion
semantics.
"""
