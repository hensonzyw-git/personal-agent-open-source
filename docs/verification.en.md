# Verification of the open-source release candidate

**Language / 语言:** [English](./verification.en.md) · [简体中文](./verification.md)

This snapshot shares code and architecture, not a directly deployable environment. Acceptance in the private production environment is separate from verification of this release candidate.

## Offline verification entry points

You need Python 3.12, uv, and Git. The Swift package needs a macOS toolchain compatible with Swift 6. First install the locked dependencies with `uv sync --locked --extra adk`. Before running tests, use a separate HOME, clear personal credential environment variables, and restrict access to private files and non-local networks at the operating-system level. A virtual environment alone does not provide that isolation.

- Python: `.venv/bin/python -m pytest tests/`
- DAL contracts: `.venv/bin/python -m pytest tests/dal/test_registry_manifest_resolution.py tests/dal/test_wave3_artifact_receipts.py`
- Swift: `swift test --package-path ios/PersonalAgentKit`
- Packaging: `uv build --no-sources`, then validate the wheel's resources and imports in an isolated environment.

Do not run deployment scripts or live/provider probes to verify a local export. Those require separate authorization and an environment you provide.

## Early snapshot preparation results (2026-09-23, before history rewriting)

**This was an unfinalized candidate.** Twenty real Finance expressions had been removed from the release history and replaced with fully synthetic public data. The full-history scan, build, and final receipt verification were still in progress, so these results alone did not establish readiness for publication.

| Check | Observation |
| --- | --- |
| Private-file and external-network isolation probe | Reading private source content and connecting to external networks were denied; the test HOME remained writable |
| Contract resources | Source and packaged copies each had 49 JSON files and were byte-identical |
| Contract rebuild receipts | The original generator had rebuilt 14 receipts on an earlier single-root candidate; the rewritten history needed new bindings |
| Wheel and sdist | Both built successfully; the wheel included the MIT license and 49 contracts; an isolated installation could load 276 migration definitions |
| Attempted full Python suite | Batch state-machine oracle replay had not completed after about 15 minutes and was stopped; no full-suite pass was claimed |
| Later batched checks | 4,707 passed, 51 failed, 5 skipped; 3 batch-oracle cases were not run |
| Temporary-directory path check | Using an isolated temporary directory without spaces, 33 relevant regressions passed, covering the original 18 failures |
| Migration-expectation calibration | Only 3 old schema-head assertions were corrected; 9 relevant regressions passed, with no migration-code changes |
| Swift | Compilation succeeded and 17 XCTest cases passed; a serial run recorded 452 Swift Testing passes before the voice-callback isolation process exited with signal 5 |
| CI | The workflow had not yet run on GitHub at that point; results after the first push appear below |

The batched results cannot be added up and reported as a full-suite pass. Thirty Python failure nodes still involved native sandbox paths in Worker or verification execution. The outer macOS isolation environment denied nested `sandbox-exec`, which was also reproduced by a minimal probe. Those tests were neither removed nor weakened; they need further validation on a compatible isolated host. Other cases requiring a tokenizer file were skipped because the operational file was not supplied.

The Swift voice source and tests matched the fixed source baseline. This round did not fix voice behavior, and the process failure was not declared an environment issue that had been resolved. Full oracle replay and Swift callback verification remained incomplete.

This round made no real-model calls, accessed no personal business-data services, deployed no server, and performed no acceptance test on a physical phone.

## Verification of the history-preserving release candidate (2026-09-23)

The fixed private baseline's 934 commits and 44 merge nodes were rewritten for privacy. Parent links, author and committer times, and identities were checked for every commit; all three categories had zero mismatches. Git dates are commit metadata, not independent proof of when the work happened.

The public Finance dataset contains 73 cases: 72 synthetic cases and 1 PRD example. Twenty are newly written `PUB-*` cases. The public reviewed-case registry is empty. Unregistered labels for real cases are still rejected; dedicated synthetic test witnesses cover tampered input, expected output, and label downgrades. Targeted contract, model-scoring, and API tests passed.

Under the rewritten history, the original generator produced 14 machine-contract rebuild receipts. Independent validation and rejection of 17 mutation classes passed. The wheel and source distribution built offline; the wheel loaded and validated all 73 public cases in an isolated installation. Across all historical objects and packaged artifacts, exact matches for the 20 known real expressions and their private digest witnesses were zero. That check covers known samples and rules; it does not mean that arbitrary personal information was detected by machine scanning.

A historical-diff scan with Gitleaks v8.30.1 produced 2,575 rule matches: 2,544 were `idempotency_key` test values in frozen contracts, 25 were in tests or fixtures, 5 were in runtime source or packaged test vectors, and 1 was in the contract generator. Another `private-key` match came from code that constructs a PEM value at test runtime; the device-authentication vector explicitly marks its private key as cross-language test material. No production credential was confirmed, but rule-based scanning does not replace human privacy review.

The earlier candidate's stalled full Python suite, 30 sandbox-related failure nodes, and Swift voice-callback crash were not closed on this release candidate. This is a reference release of source and architecture, not evidence of complete deployment or physical-device acceptance.

## First GitHub CI runs (2026-09-23)

The repository remains private. The [offline checks for commit `a41c1de`](https://github.com/hensonzyw-git/personal-agent-open-source/actions/runs/35819619834) ran: the Swift job passed, while the Python job reported 20 collection errors caused by `ModuleNotFoundError: No module named 'tests'` before test cases could run. The earlier initial push also failed. These results do not establish a passing CI or Python suite, or show that the 30 sandbox-related failure nodes have been resolved.
