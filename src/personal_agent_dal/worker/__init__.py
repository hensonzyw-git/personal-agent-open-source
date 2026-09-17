"""The runnable Home Mac Worker (DAL-016/017/019/020 runtime).

The G2 frozen gate closed on the *pure policy* half of the worker — the guards
and lease/epoch decisions. This package is the execution half those decisions
govern: a durable job queue (`queue`), a deterministic toolchain registry and
executor (`toolchain`), a checkpoint/handoff bundle (`checkpoint`), the worker
config with its repo allowlist (`config`), and the single poll cycle
(`poll_once`) wired to a launchd one-shot CLI (`cli`).
"""
