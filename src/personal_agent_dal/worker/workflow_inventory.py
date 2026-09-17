"""Encrypted v3 attempts sharing a workflow-owned reservation, never legacy jobs."""
import json
from personal_agent_core.manifest import canonical_json
from personal_agent_dal.worker.runtime_inventory import RuntimeInventory,TRANSITIONS
from personal_agent_dal.worker.supervisor import SupervisorRefusal,_digest


class WorkflowInventory(RuntimeInventory):
    def __init__(self,supervisor,keyring):
        super().__init__(supervisor)
        self.keyring=keyring
        with supervisor._lock(),supervisor._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS workflow_runtime_inventory (effective_attempt TEXT PRIMARY KEY, reservation_id TEXT NOT NULL REFERENCES reservations(id), source_digest TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, observation TEXT NOT NULL, result TEXT, response TEXT)')

    def _seal(self,attempt,column,value):
        return canonical_json(self.keyring.encrypt(canonical_json(value).encode(),table='workflow_runtime_inventory',column=column,row_id=attempt))

    def _open(self,attempt,column,value):
        return json.loads(self.keyring.decrypt(json.loads(value),table='workflow_runtime_inventory',column=column,row_id=attempt))

    def _get(self,db,attempt):
        db.row_factory=__import__('sqlite3').Row
        row=db.execute('SELECT * FROM workflow_runtime_inventory WHERE effective_attempt=?',(attempt,)).fetchone()
        if not row:return None
        row=dict(row)
        for column in ('binding','observation','result','response'):
            row[column]=self._open(attempt,column,row[column]) if row[column] is not None else None
        return row

    def adopt(self,context,reservation):
        attempt=context['attempt_id']
        if (context.get('owner',{}).get('kind')!='workflow' or reservation['authority'].get('workflow_id')!=context['owner'].get('workflow_id')):raise SupervisorRefusal('WORKFLOW_OWNER_REQUIRED')
        with self.supervisor._lock(),self.supervisor._db() as db:
            self.supervisor._validate(db,reservation['reservation_id'])
            prior=self._get(db,attempt)
            if prior:
                if prior['binding']!=context:raise SupervisorRefusal('RUNTIME_BINDING_CONFLICT')
                return prior
            db.execute('INSERT INTO workflow_runtime_inventory VALUES (?,?,?,?,?,1,?,NULL,NULL)',
                (attempt,reservation['reservation_id'],_digest(reservation),self._seal(attempt,'binding',context),'prepared',self._seal(attempt,'observation',{})))
            return self._get(db,attempt)

    def transition(self,attempt,expected,target,*,observation=None,result=None,response=None):
        if target not in TRANSITIONS[expected] and not (expected=='unknown' and target=='refused'):raise SupervisorRefusal('RUNTIME_TRANSITION_INVALID')
        with self.supervisor._lock(),self.supervisor._db() as db:
            row=self._get(db,attempt)
            if row is None or row['state']!=expected:raise SupervisorRefusal('RUNTIME_CAS_LOST')
            if target=='starting':
                others=db.execute("SELECT effective_attempt FROM workflow_runtime_inventory WHERE effective_attempt<>? AND state IN ('starting','running','unknown')",(attempt,)).fetchall()
                if others:
                    raise SupervisorRefusal('UNRESOLVED_PROCESS_OWNERSHIP')
                if db.execute("SELECT 1 FROM runtime_inventory WHERE state IN ('starting','running','unknown') LIMIT 1").fetchone():
                    raise SupervisorRefusal('LEGACY_PROCESS_OWNERSHIP_UNRESOLVED')
            obs=dict(row['observation']);obs.update(observation or {})
            db.execute('UPDATE workflow_runtime_inventory SET state=?,version=version+1,observation=?,result=COALESCE(?,result),response=COALESCE(?,response) WHERE effective_attempt=? AND version=?',
                (target,self._seal(attempt,'observation',obs),self._seal(attempt,'result',result) if result is not None else None,
                 self._seal(attempt,'response',response) if response is not None else None,attempt,row['version']))
            return self._get(db,attempt)

    def page(self,*,after='',limit=64):
        if type(limit) is not int or not 1<=limit<=64:raise SupervisorRefusal('RUNTIME_PAGE_LIMIT')
        with self.supervisor._db() as db:
            ids=[r[0] for r in db.execute("SELECT effective_attempt FROM workflow_runtime_inventory WHERE effective_attempt>? AND state NOT IN ('reported','refused') ORDER BY effective_attempt LIMIT ?",(after,limit))]
            return [self._get(db,attempt) for attempt in ids]

    def observe(self,attempt,observation):
        with self.supervisor._lock(),self.supervisor._db() as db:
            row=self._get(db,attempt)
            if row is None or row['state'] not in ('starting','running','unknown'):raise SupervisorRefusal('RUNTIME_CAS_LOST')
            value=dict(row['observation']);value.update(observation)
            db.execute('UPDATE workflow_runtime_inventory SET observation=?,version=version+1 WHERE effective_attempt=? AND version=?',
                (self._seal(attempt,'observation',value),attempt,row['version']))

    def execution_reservation(self,attempt):
        from pathlib import Path
        row=self.get(attempt)
        if row is None:raise SupervisorRefusal('RUNTIME_PREPARATION_REQUIRED')
        body=self.supervisor.validate(row['reservation_id'])
        scratch=Path(body['temp'])/attempt
        scratch.mkdir(mode=0o700,exist_ok=True)
        self.supervisor._private(scratch)
        return dict(body,temp=str(scratch))

    def renew_reservation(self,workflow_id,binding):
        """A fresh server lease can rebind stopped physical directories after boot.

        Historical attempt bindings stay encrypted and immutable. This updates
        only the reusable reservation's supervisor identity after inode checks.
        """
        import copy
        from datetime import datetime,timezone
        if ((binding.get('boot_id'),binding.get('supervisor_epoch'))!=(self.supervisor.boot_id,self.supervisor.epoch)
            or binding.get('owner')!={'kind':'workflow','workflow_id':workflow_id}
            or datetime.fromisoformat(binding['lease_until'])<=datetime.now(timezone.utc)):
            raise SupervisorRefusal('WORKFLOW_RENEWAL_AUTHORITY_INVALID')
        with self.supervisor._lock(),self.supervisor._db() as db:
            saved=db.execute('SELECT body FROM reservations WHERE attempt=?',('workflow:'+workflow_id,)).fetchone()
            if saved is None:return
            body=json.loads(saved[0])
            if (body['boot_id'],body['supervisor_epoch'])==(self.supervisor.boot_id,self.supervisor.epoch):return
            if body['authority']!={'workflow_id':workflow_id}:raise SupervisorRefusal('WORKFLOW_OWNER_REQUIRED')
            for table in ('workflow_runtime_inventory','runtime_inventory'):
                if db.execute('SELECT 1 FROM '+table+" WHERE state IN ('dispatch_requested','granted','starting','running','unknown','result_ready') LIMIT 1").fetchone():
                    raise SupervisorRefusal('UNRESOLVED_PROCESS_OWNERSHIP')
            historical=copy.copy(self.supervisor)
            historical.boot_id=body['boot_id'];historical.epoch=body['supervisor_epoch']
            historical._validate(db,body['reservation_id'])
            body.update(boot_id=self.supervisor.boot_id,supervisor_epoch=self.supervisor.epoch)
            db.execute('UPDATE reservations SET body=? WHERE id=?',(canonical_json(body),body['reservation_id']))
