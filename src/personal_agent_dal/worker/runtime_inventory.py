"""Supervisor runtime schema 2: immutable source reservations, CAS observations.

Transactions contain SQLite work only. Filesystem provisioning, transport and
process operations belong to the caller, outside these transactions.
"""
import json
import os
import fcntl
import stat
from contextlib import contextmanager
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.worker.supervisor import SupervisorRefusal, _digest

TRANSITIONS = {
    'prepared': {'dispatch_requested','refused'},
    'dispatch_requested': {'granted','unknown','refused'},
    'granted': {'starting','unknown','refused'},
    'starting': {'running','unknown'},
    'running': {'result_ready','unknown'},
    'result_ready': {'reported'},
    'reported': set(), 'unknown': set(), 'refused': set(),
}

class RuntimeInventory:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        with supervisor._lock(), supervisor._db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 2): raise SupervisorRefusal('RUNTIME_SCHEMA_UNSUPPORTED')
            db.execute('CREATE TABLE IF NOT EXISTS runtime_inventory (effective_attempt TEXT PRIMARY KEY, reservation_id TEXT NOT NULL UNIQUE REFERENCES reservations(id), source_digest TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, observation TEXT NOT NULL, result TEXT, response TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS runtime_provisioning (reservation_id TEXT PRIMARY KEY REFERENCES reservations(id), body TEXT NOT NULL)')
            # Historical launch_inventory remains untouched and participates in adoption checks.
            db.execute('PRAGMA user_version=2')

    def adopt(self, context, reservation):
        s = self.supervisor
        isolation = context['isolation']
        if isolation and (context['isolation_reserved_by'] != context['attempt_id'] or
            isolation['new_reservation_id'] != reservation['reservation_id'] or
            isolation['new_inventory_sha256'] != _digest(reservation) or
            isolation['new_workspace_id'] != reservation['workspace_id'] or
            isolation['workspace_generation'] != reservation['generation']):
            raise SupervisorRefusal('SOURCE_RESERVATION_MISMATCH')
        binding = canonical_json(context)
        with s._lock(), s._db() as db:
            s._validate(db, reservation['reservation_id'])
            prior = db.execute('SELECT binding FROM runtime_inventory WHERE effective_attempt=?', (context['attempt_id'],)).fetchone()
            if prior:
                if prior[0] != binding: raise SupervisorRefusal('RUNTIME_BINDING_CONFLICT')
                return self._get(db, context['attempt_id'])
            if db.execute('SELECT 1 FROM launch_inventory WHERE reservation_id=?', (reservation['reservation_id'],)).fetchone():
                raise SupervisorRefusal('LEGACY_SYNTHETIC_RESERVATION_USED')
            if db.execute('SELECT 1 FROM runtime_inventory WHERE reservation_id=?', (reservation['reservation_id'],)).fetchone():
                raise SupervisorRefusal('RESERVATION_ALREADY_ADOPTED')
            db.execute('INSERT INTO runtime_inventory VALUES (?,?,?,?,?,1,?,NULL,NULL)',
                (context['attempt_id'],reservation['reservation_id'],_digest(reservation),binding,'prepared','{}'))
            return self._get(db, context['attempt_id'])

    def _get(self, db, attempt):
        db.row_factory = __import__('sqlite3').Row
        row = db.execute('SELECT * FROM runtime_inventory WHERE effective_attempt=?',(attempt,)).fetchone()
        if not row: return None
        result = dict(row)
        for k in ('binding','observation','result','response'):
            result[k] = json.loads(result[k]) if result[k] is not None else None
        return result

    def get(self, attempt):
        with self.supervisor._db() as db: return self._get(db, attempt)

    def transition(self, attempt, expected, target, *, observation=None, result=None, response=None):
        if target not in TRANSITIONS[expected]: raise SupervisorRefusal('RUNTIME_TRANSITION_INVALID')
        with self.supervisor._lock(), self.supervisor._db() as db:
            row = self._get(db, attempt)
            if row is None or row['state'] != expected: raise SupervisorRefusal('RUNTIME_CAS_LOST')
            obs = dict(row['observation']); obs.update(observation or {})
            # One unresolved execution blocks every other launch on this Worker.
            if target == 'starting':
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_runtime_inventory'").fetchone() and db.execute("SELECT 1 FROM workflow_runtime_inventory WHERE state IN ('starting','running','unknown') LIMIT 1").fetchone():
                    raise SupervisorRefusal('WORKFLOW_PROCESS_OWNERSHIP_UNRESOLVED')
                others = db.execute("SELECT state,observation FROM runtime_inventory WHERE effective_attempt<>? AND state IN ('starting','running','unknown')", (attempt,)).fetchall()
                if any(state != 'unknown' or not json.loads(obs).get('reconciliation_stop', {}).get('process_exited') for state, obs in others):
                    raise SupervisorRefusal('UNRESOLVED_PROCESS_OWNERSHIP')
            db.execute('UPDATE runtime_inventory SET state=?,version=version+1,observation=?,result=COALESCE(?,result),response=COALESCE(?,response) WHERE effective_attempt=? AND version=?',
                (target,canonical_json(obs),canonical_json(result) if result is not None else None,canonical_json(response) if response is not None else None,attempt,row['version']))
            return self._get(db, attempt)

    @contextmanager
    def owner(self, attempt):
        """Kernel-held lifetime lock: crash releases it, concurrent polls skip it.

        Independent opens conflict even in one process. No database transaction
        spans process or transport work; the short inventory CAS remains separate.
        """
        self.supervisor._private(self.supervisor.root)
        path = self.supervisor.root / ('runtime-owner-' + _digest(attempt))
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
                raise SupervisorRefusal('RUNTIME_OWNER_INVALID')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SupervisorRefusal('RUNTIME_OWNER_ACTIVE') from None
            yield
        finally:
            os.close(fd)

    def page(self, *, after='', limit=64):
        if type(limit) is not int or not 1 <= limit <= 64:
            raise SupervisorRefusal('RUNTIME_PAGE_LIMIT')
        with self.supervisor._db() as db:
            ids = [r[0] for r in db.execute(
                "SELECT effective_attempt FROM runtime_inventory WHERE effective_attempt>? "
                "AND state NOT IN ('reported','refused') ORDER BY effective_attempt LIMIT ?",
                (after, limit))]
            return [self._get(db, attempt) for attempt in ids]
