"""Server-local public Supervisor registration. No key generation or remote calls."""
import argparse
import json
import os
import stat
from pathlib import Path
from personal_agent_dal.machine.isolation_evidence import register_supervisor
from personal_agent_dal.storage.engine import create_database_engine


def main(argv=None):
    parser = argparse.ArgumentParser(prog='personal-agent-dal-deploy')
    parser.add_argument('--database', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    register = sub.add_parser('register-supervisor')
    for name in ('kid', 'worker-id', 'machine-id', 'boot-id'):
        register.add_argument('--'+name, required=True)
    register.add_argument('--supervisor-epoch', required=True, type=int)
    register.add_argument('--public-key-file', required=True, type=Path)
    args = parser.parse_args(argv)
    # Only the database owner (or deployment root) may provision identities.
    try:
        info = args.database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (os.getuid(),) and os.getuid() != 0:
            raise ValueError('DATABASE_OWNER_REQUIRED')
        key = args.public_key_file.read_text(encoding='utf-8').strip()
        if len(key) > 4096 or 'PRIVATE' in key:
            raise ValueError('PUBLIC_KEY_REQUIRED')
        engine = create_database_engine(args.database)
        try:
            epoch = register_supervisor(engine, kid=args.kid, worker_id=args.worker_id,
                machine_id=args.machine_id, boot_id=args.boot_id,
                supervisor_epoch=args.supervisor_epoch, public_key=key)
            from personal_agent_dal.service.app import _append_redacted_audit
            _append_redacted_audit(engine, event_type='deployment.supervisor.register', outcome='accepted')
        finally:
            engine.dispose()
    except (ValueError, OSError):
        parser.exit(1, 'Supervisor public registration refused\n')
    print(json.dumps(dict(kid=args.kid, registration_epoch=epoch, supervisor_epoch=args.supervisor_epoch)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
