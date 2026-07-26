"""Administrator device management (`personal-agent-device`).

`DEV-029`. Design 5.1 is explicit that minting an enrollment code, revoking a
device and adjusting scopes are **operator** actions on the server, and that no
public administrator API is added for them. This is that operator surface: it
runs on the host, against the Agent database, over SSH.

Three deliberate properties:

- **the code is shown once and never stored.** The database holds a SHA-256 of
  it, so this is the only moment it exists in readable form. It is printed to
  stdout and nowhere else -- not into the log, not into an argument of another
  command -- and it expires in ten minutes.
- **`device.manage` is never inferred.** Design 4.1 says the first device does
  not get it automatically; it takes an explicit flag on the code that device
  will claim.
- **a scope must exist to be granted.** `set-scopes` validates against the
  device scopes and the scopes the current tool manifest actually declares, so a
  typo produces a refusal instead of a device that silently holds nothing.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Final

from personal_agent.api.device_api import SELF_READ_SCOPE, SELF_REVOKE_SCOPE
from personal_agent.auth.enrollment import (
    DEFAULT_DEVICE_SCOPES,
    DEVICE_MANAGE_SCOPE,
    create_enrollment_code,
    decode_device_scopes,
    encode_device_scopes,
    revoke_device,
)
from personal_agent.storage.engine import (
    check_integrity,
    create_database_engine,
    session_factory,
)
from personal_agent.storage.models import Device
from personal_agent_core.manifest import load_manifest
from personal_agent_core.timeutil import utc_now


#: Scopes the device surface itself defines, beside the tool scopes below.
DEVICE_SURFACE_SCOPES: Final[frozenset[str]] = frozenset(
    {SELF_READ_SCOPE, SELF_REVOKE_SCOPE, DEVICE_MANAGE_SCOPE}
)


def known_scopes() -> frozenset[str]:
    """Every scope a device could legitimately hold on this build."""
    manifest = load_manifest()
    declared = {
        scope
        for tool in manifest["tools"]
        for scope in tool.get("required_scopes", ())
    }
    return frozenset(declared | DEVICE_SURFACE_SCOPES | set(DEFAULT_DEVICE_SCOPES))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Operator device management for the Agent API: mint an enrollment "
            "code, list devices, revoke one, or set its scopes."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser(
        "issue-code",
        help="Mint a single-use, 10-minute enrollment code and print it once.",
    )
    issue.add_argument(
        "--grants-device-manage",
        action="store_true",
        help=(
            "Let the device that claims this code manage other devices. Design "
            "4.1: never granted implicitly, not even to the first device."
        ),
    )

    sub.add_parser("list", help="List devices. No key material is printed.")

    revoke = sub.add_parser("revoke", help="Revoke a device immediately.")
    revoke.add_argument("--device-id", required=True)

    scopes = sub.add_parser(
        "set-scopes", help="Replace a device's scopes with an explicit set."
    )
    scopes.add_argument("--device-id", required=True)
    scopes.add_argument(
        "--scope",
        action="append",
        default=[],
        help="Repeatable. The resulting set replaces the current one.",
    )

    args = parser.parse_args()

    if not args.database.exists():
        # Creating one here would produce an empty device registry that looks
        # like every device was revoked.
        raise SystemExit(f"no Agent database at {args.database}")

    engine = create_database_engine(args.database)
    check_integrity(engine)
    sessions = session_factory(engine)
    now = utc_now()
    try:
        with sessions() as session:
            try:
                if args.command == "issue-code":
                    _issue_code(session, now=now, manage=args.grants_device_manage)
                elif args.command == "list":
                    _list(session)
                elif args.command == "revoke":
                    _revoke(session, device_id=args.device_id, now=now)
                elif args.command == "set-scopes":
                    _set_scopes(session, device_id=args.device_id, scopes=args.scope)
                session.commit()
            except Exception:
                session.rollback()
                raise
    finally:
        engine.dispose()


def _issue_code(session, *, now, manage: bool) -> None:
    issued = create_enrollment_code(session, now=now, grants_device_manage=manage)
    print(f"enrollment code: {issued.code}")
    print(f"expires at:      {issued.expires_at.isoformat()}")
    print(f"grants manage:   {manage}")
    print(
        "Type it into the iPhone app once. It is single-use, it is not stored "
        "in readable form, and it cannot be shown again."
    )


def _list(session) -> None:
    devices = (
        session.query(Device).order_by(Device.created_at.asc(), Device.device_id).all()
    )
    if not devices:
        print("no devices enrolled")
        return
    for device in devices:
        scopes = ",".join(decode_device_scopes(device.scopes))
        print(
            f"{device.device_id}  {device.status:<8} "
            f"push={'yes' if device.encrypted_push_token is not None else 'no'}  "
            f"tools_version={device.allowed_tools_version}  "
            f"{device.display_name}"
        )
        print(f"    scopes: {scopes}")


def _revoke(session, *, device_id: str, now) -> None:
    if session.get(Device, device_id) is None:
        raise SystemExit(f"no such device {device_id}")
    if revoke_device(session, device_id=device_id, now=now):
        print(f"revoked {device_id}")
    else:
        print(f"{device_id} was already revoked")


def _set_scopes(session, *, device_id: str, scopes: list[str]) -> None:
    device = session.get(Device, device_id)
    if device is None:
        raise SystemExit(f"no such device {device_id}")
    requested = sorted(set(scopes))
    if not requested:
        raise SystemExit(
            "--scope is required: an empty scope set is expressed by revoking "
            "the device, not by leaving it enrolled with no authority"
        )
    unknown = sorted(set(requested) - known_scopes())
    if unknown:
        raise SystemExit(
            f"unknown scopes {unknown}; a typo here would silently grant nothing"
        )
    before = decode_device_scopes(device.scopes)
    device.scopes = encode_device_scopes(requested)
    print(f"{device_id} scopes: {','.join(before)} -> {','.join(requested)}")
    print(
        "Existing access tokens keep the scopes they were minted with for up to "
        "10 minutes; the next token carries the new set."
    )


if __name__ == "__main__":
    main()
