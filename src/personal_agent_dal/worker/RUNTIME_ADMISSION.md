# Runtime admission contract

`dal.worker-config/2.1` uses the 2.0 execution fields plus absolute
`admission_ref`; it does **not** accept `production_enabled`. Version 2.0 and
historical routes retain their refusal. Worker, supervisor, adapter and admission
JSON files must be canonical absolute, owner-only regular files outside all task
read/write roots. No product profile is selected or registered by this code.

The closed `dal.runtime-admission/1.0` object contains:

- `code_sha256`: `runtime_admission.code_identity()` over worker Python sources;
  `policy`: `trusted-single-user/1.0`.
- `identity`: exact supervisor signing identity, including machine/worker,
  registration/supervisor epochs and current OS boot ID.
- `config_refs`: absolute worker/supervisor/adapter paths mapped to their canonical
  JSON SHA-256; exact loaded pins and routes are also bound into the launch plan.
- `issued_at`, `expires_at`: integer Unix seconds; `revoked_at` must be null.
- `scope`: `single-role-report-only`; `provenance`:
  `operator-attested-external-native-cli`.
- `roles`: exactly planner, coder, reviewer. Each contains the complete
  `configuration`, `plan` from `plan_contract()`, and `smoke`.

Each smoke contains actual `command` argv and canonical `command_sha256`,
`task_paths` (workspace/temp/git paths used by the external probe), `started_at`,
`ended_at` (at most 120 seconds apart), `scope=synthetic-files-only`,
`provenance=external-native-cli`, `result` and its canonical `result_sha256`.
The command must normalize to the exact generated argv template. Result fields
are `exit_code=0`, exact `cli_version`, retained stdout/stderr SHA-256, and ordered
observations: report-produced, scratch-write, business-write-denied for read-only
roles; report-produced, scratch-write, source-edit, git-add, git-commit for coder.
No wildcard, fixture issuer, arbitrary argv or standalone passed boolean is valid.

Bootstrap probes run outside production dispatch against synthetic files using
the same generated native CLI plan. Admission does not run probes and therefore
has no circular dispatch dependency. The operator must inspect and retain the
actual probe evidence before attesting it. Hashes validate record consistency;
they cannot prove that an owner fabricated no evidence. This is the approved
trusted-single-user operator boundary, not cryptographic independent provenance.
Offline test records must never be installed as admission records.

Admission digest is included in LaunchPlan and the signed launcher-plan digest.
The manifest's isolation policy digest describes the applied argv/environment/
read/write plan. Evidence is reread before key access/signing, dispatch, Popen,
and heartbeat; changing a config, pin, source, boot or evidence requires fresh
admission. Removal, expiry or revocation stops continued authority.

No live smoke or deployment is claimed by this implementation. Existing supported
subscription routes are retained; K3 and other unsupported routes still refuse.
Result artifacts remain local unless explicitly uploaded elsewhere. Task-produced
test reports are labeled unverified; absent evidence is never a test pass. A
surviving process group without proven ownership yields unknown and cannot launch
again; resending its saved result is evidence delivery, not execution replay.
