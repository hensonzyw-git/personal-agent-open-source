"""`DEV-035`: CLI for the library-side restore checks.

Run by ``scripts/restore_drill.sh`` against a restored Agent database. Loads
the data key ring from the environment (the off-machine secret-store copy, not
anything on the ECS), runs the Agent and Finance restore gates, and exits
non-zero if any failed.

The manifest is the export produced by
``personal-agent-export-deletion-manifest``; the AEAD sample entry id names one
row in the restored ``deletion_manifest`` table to decrypt as the fixed sample.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from personal_agent.backup.restore_verify import run_all
from personal_agent.keys import load_agent_data_keyring


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the DEV-035 library-side restore checks on a restored DB."
    )
    parser.add_argument("--agent-database", type=Path, required=True)
    parser.add_argument("--finance-database", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="the deletion-manifest export JSON to replay",
    )
    parser.add_argument(
        "--media-bundle", type=Path, required=True,
        help="the verified restored media run containing ciphertext files",
    )
    parser.add_argument(
        "--aead-sample-entry-id",
        default=None,
        help=(
            "a deletion_manifest entry_id to decrypt as the fixed sample. "
            "Required for the G5 drill evidence (解密样本); omit only for a "
            "partial pre-G5 smoke when no sample is seeded yet."
        ),
    )
    args = parser.parse_args()

    keyring = load_agent_data_keyring()
    manifest_payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_entries = (
        manifest_payload["entries"]
        if isinstance(manifest_payload, dict) and isinstance(manifest_payload.get("entries"), list)
        else manifest_payload
    )
    if not isinstance(manifest_entries, list):
        raise SystemExit("deletion manifest is neither a legacy list nor a bundle entries object")

    results = run_all(
        args.agent_database,
        keyring,
        finance_database=args.finance_database,
        manifest_entries=manifest_entries,
        aead_sample_entry_id=args.aead_sample_entry_id,
        media_bundle=args.media_bundle,
    )

    failed = 0
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        print(f"{status}  {r['name']}: {r['detail']}")
        if not r["ok"]:
            failed += 1

    if failed:
        print(f"\n{failed} restore check(s) failed", file=sys.stderr)
        sys.exit(1)
    print("\nall restore checks passed")


if __name__ == "__main__":
    main()
