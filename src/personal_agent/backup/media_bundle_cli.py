"""The two least-privilege commands around a media backup bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from personal_agent.backup.media_bundle import (
    MediaBundleError,
    prepare_media_bundle,
    verify_media_bundle_run,
    verify_published_media_bundle,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or verify media backup bundle")
    subparsers = parser.add_subparsers(dest="action", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--database", type=Path, required=True)
    import os
    prepare.add_argument("--media-root", type=Path,
                         default=os.environ.get("PERSONAL_AGENT_MEDIA_ROOT") or None)
    prepare.add_argument("--stage-root", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    group = verify.add_mutually_exclusive_group(required=True)
    group.add_argument("--stage-root", type=Path)
    group.add_argument("--run", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare_media_bundle(
                database=args.database, media_root=args.media_root, stage_root=args.stage_root
            )
        else:
            result = (
                verify_published_media_bundle(args.stage_root)
                if args.stage_root is not None
                else verify_media_bundle_run(args.run)
            )
    except MediaBundleError as exc:
        raise SystemExit(f"media backup bundle refused: {exc}") from exc
    print(json.dumps({"run_id": result.run_id, "path": str(result.path)}, sort_keys=True))


if __name__ == "__main__":
    main()
