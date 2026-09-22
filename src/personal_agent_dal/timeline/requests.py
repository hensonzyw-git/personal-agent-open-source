"""Encrypted intake, idempotent commands and stable complete request snapshots.

This module never enqueues a Job or registers a Feature. Project selection and
explicit execution admission are separate, later transitions.
"""
import base64
import hashlib
import hmac
import json
import re
from datetime import timedelta

from sqlalchemy import select, update

from personal_agent_core.crypto import CryptoError
from personal_agent_core.ids import new_id
from personal_agent_core.manifest import canonical_json
from personal_agent_core.sqlite import run_write_transaction
from personal_agent_core.timeutil import utc_now
from personal_agent_dal.storage.audit import append_audit_event
from personal_agent_dal.storage.engine import session_factory
from personal_agent_dal.storage.timeline_models import (
    DevelopmentCommand, DevelopmentEvent, DevelopmentEventStream,
    DevelopmentQuerySnapshot, DevelopmentRequest, DevelopmentRequestRevision, DevelopmentWorkflow,
)


class TimelineRefusal(ValueError):
    """Safe closed rejection code; never carries the input body."""


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise TimelineRefusal('INVALID_ARGUMENT')


class RequestService:
    def __init__(self, engine, *, keyring, cursor_key, allowed_repository_ids=(), now=utc_now):
        if keyring is None:
            raise TimelineRefusal('INPUT_DECRYPT_UNAVAILABLE')
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 32:
            raise TimelineRefusal('CURSOR_KEY_INVALID')
        self.engine, self.sessions = engine, session_factory(engine)
        self.keyring, self.cursor_key, self.now = keyring, cursor_key, now
        self.allowed_repository_ids = frozenset(allowed_repository_ids)

    def _seal(self, model, row_id, column, value):
        return self.keyring.encrypt(canonical_json(value).encode(),
            table=model.__tablename__, column=column, row_id=row_id)

    def _open(self, model, row_id, column, value):
        try:
            return json.loads(self.keyring.decrypt(value, table=model.__tablename__,
                column=column, row_id=row_id))
        except (CryptoError, ValueError, TypeError, UnicodeError):
            raise TimelineRefusal('INPUT_INTEGRITY_FAILED') from None

    def submit(self, *, command_id, subject, source_message_ref, body):
        for value in (command_id, subject, source_message_ref):
            valid_id(value)
        try:
            valid_body = isinstance(body, str) and bool(body.strip()) and len(body.encode()) <= 32768
        except UnicodeError:
            valid_body = False
        if not valid_body:
            raise TimelineRefusal('INVALID_ARGUMENT')
        fingerprint = digest(dict(subject=subject, source_message_ref=source_message_ref, body=body))
        def work(s):
            old = s.scalar(select(DevelopmentCommand).where(DevelopmentCommand.command_id == command_id))
            if old:
                if old.body_sha256 != fingerprint:
                    raise TimelineRefusal('IDEMPOTENCY_CONFLICT')
                return self._open(DevelopmentCommand, command_id, 'sealed_result', old.sealed_result)
            request = s.scalar(select(DevelopmentRequest).where(DevelopmentRequest.source_message_ref == source_message_ref))
            if request:
                if request.request_sha256 != fingerprint:
                    raise TimelineRefusal('IDEMPOTENCY_CONFLICT')
                result = self._receipt(request)
            else:
                now = self.now()
                request = DevelopmentRequest(request_id=new_id(), source_message_ref=source_message_ref,
                    request_sha256=fingerprint, version=1, status='clarifying', created_at=now)
                s.add(request)
                s.flush()
                revision_id = new_id()
                s.add(DevelopmentRequestRevision(revision_id=revision_id, request_id=request.request_id,
                    revision=1, body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    sealed_body=self._seal(DevelopmentRequestRevision, revision_id, 'sealed_body', {'text':body})))
                s.add(DevelopmentWorkflow(workflow_id=request.request_id, request_id=request.request_id,
                    feature_id=None, contract_version='dal.timeline-workflow/1.0', version=1,phase='clarify',status='active'))
                result = self._receipt(request)
                self._append_event(s, request, 'request.accepted', result)
                append_audit_event(s, event_id=new_id(), trace_id=request.request_id,
                    event_type='development.request.accepted', redacted_summary='accepted_not_started', now=now)
            s.add(DevelopmentCommand(command_id=command_id, body_sha256=fingerprint,
                sealed_result=self._seal(DevelopmentCommand, command_id, 'sealed_result', result)))
            return result
        with self.sessions() as s:
            return run_write_transaction(s, lambda: work(s), attempts=8)

    @staticmethod
    def _receipt(request):
        return dict(schema_version='dal.timeline/1.0', request_id=request.request_id,
            version=request.version, status='accepted_not_started', feature_id=None)

    def _append_event(self, s, request, kind, body):
        stream = s.scalar(select(DevelopmentEventStream))
        if stream is None:
            stream_id = new_id()
            stream = DevelopmentEventStream(stream_id=stream_id, singleton=1, next_seq=1,
                tail_digest=digest({'domain':'dal.timeline-genesis/1.0', 'stream_id':stream_id}))
            s.add(stream)
            s.flush()
        seq, previous = stream.next_seq, stream.tail_digest
        event_id = new_id()
        body_digest = digest(body)
        event_digest = digest(dict(stream_id=stream.stream_id, seq=seq, event_id=event_id,
            request_id=request.request_id, version=request.version, kind=kind,
            body_digest=body_digest, prev_digest=previous))
        count = s.execute(update(DevelopmentEventStream).where(
            DevelopmentEventStream.stream_id == stream.stream_id,
            DevelopmentEventStream.next_seq == seq,
        ).values(next_seq=seq+1, tail_digest=event_digest)).rowcount
        if count != 1:
            raise TimelineRefusal('EVENT_STREAM_CONFLICT')
        s.add(DevelopmentEvent(event_id=event_id, stream_id=stream.stream_id, seq=seq,
            kind=kind, request_id=request.request_id, version=request.version,
            body_digest=body_digest, prev_digest=previous, digest=event_digest,
            sealed_body=self._seal(DevelopmentEvent, event_id, 'sealed_body', body)))

    def detail(self, request_id):
        valid_id(request_id)
        with self.sessions() as s:
            request = s.scalar(select(DevelopmentRequest).where(DevelopmentRequest.request_id == request_id))
            if request is None:
                raise TimelineRefusal('REQUEST_NOT_FOUND')
            return self._detail(s, request)

    def _detail(self, s, request):
        revision = s.scalar(select(DevelopmentRequestRevision).where(
            DevelopmentRequestRevision.request_id == request.request_id,
            DevelopmentRequestRevision.revision == request.version))
        if revision is None:
            raise TimelineRefusal('INPUT_INTEGRITY_FAILED')
        body = self._open(DevelopmentRequestRevision, revision.revision_id, 'sealed_body', revision.sealed_body)
        if not isinstance(body, dict) or set(body) != {'text'} or not isinstance(body['text'], str):
            raise TimelineRefusal('INPUT_INTEGRITY_FAILED')
        if hashlib.sha256(body['text'].encode()).hexdigest() != revision.body_sha256:
            raise TimelineRefusal('INPUT_INTEGRITY_FAILED')
        workflow = s.scalar(select(DevelopmentWorkflow).where(
            DevelopmentWorkflow.request_id == request.request_id))
        if workflow is None:
            raise TimelineRefusal('INPUT_INTEGRITY_FAILED')
        if workflow.feature_id is not None:
            from personal_agent_dal.storage.models import Feature
            repository_id = s.scalar(select(Feature.repository_id).where(Feature.feature_id == workflow.feature_id))
            if repository_id not in self.allowed_repository_ids:
                from personal_agent_dal.storage.timeline_models import DevelopmentProjectBinding, DevelopmentProjectAuthorization
                binding=s.get(DevelopmentProjectBinding,workflow.workflow_id)
                grant=s.get(DevelopmentProjectAuthorization,binding.grant_id) if binding else None
                if (grant is None or binding.project_id!=repository_id or grant.project_id!=repository_id
                    or grant.revoked or grant.expires_at<=self.now()):raise TimelineRefusal('REQUEST_NOT_FOUND')
        from personal_agent_dal.storage.timeline_models import DevelopmentArtifact, DevelopmentDecisionRequest, DevelopmentStage, DevelopmentStagePlan
        artifacts=[dict(artifact_id=a.artifact_id,kind=a.kind,revision=a.revision,body_sha256=a.body_sha256) for a in s.scalars(select(DevelopmentArtifact).where(DevelopmentArtifact.workflow_id==workflow.workflow_id).order_by(DevelopmentArtifact.kind,DevelopmentArtifact.revision))]
        decisions=[dict(decision_id=d.decision_id,kind=d.kind,version=d.version,expires_at=d.expires_at.isoformat(),artifact_id=self._open(DevelopmentDecisionRequest,d.decision_id,'sealed_binding',d.sealed_binding)['artifact_id']) for d in s.scalars(select(DevelopmentDecisionRequest).where(DevelopmentDecisionRequest.workflow_id==workflow.workflow_id,DevelopmentDecisionRequest.status=='pending',DevelopmentDecisionRequest.expires_at>self.now()))]
        from personal_agent_dal.timeline.commit_reviews import review_summary
        stages=[]
        for stage in s.scalars(select(DevelopmentStage).join(DevelopmentStagePlan).where(DevelopmentStagePlan.workflow_id==workflow.workflow_id).order_by(DevelopmentStagePlan.revision,DevelopmentStage.ordinal)):
            goal=self._open(DevelopmentStage,stage.stage_id+':'+str(stage.revision),'sealed_goal',stage.sealed_goal)
            stages.append(dict(stage_id=stage.stage_id,revision=stage.revision,state=stage.state,state_version=stage.state_version,
                base_sha=stage.base_sha,head_sha=stage.head_sha,tree_sha=stage.tree_sha,verification_digest=stage.verification_digest,
                review_digest=stage.review_digest,commit_digest=stage.commit_digest,commit_subject=goal.get('commit',{}).get('subject'),
                review_summary=review_summary(self,s,workflow.workflow_id,stage)))
        return dict(request_id=request.request_id, request_version=request.version,
            version=workflow.version, status=workflow.status, text=body['text'],
            created_at=request.created_at.isoformat(), feature_id=workflow.feature_id,
            phase=workflow.phase, artifacts=artifacts, pending_decisions=decisions, stages=stages,
            execution_started=False if workflow.phase == 'clarify' else None)

    def _task_items(self, s, view):
        from personal_agent_dal.storage.models import Feature
        workflows = list(s.scalars(select(DevelopmentWorkflow)))
        owned_features = {w.feature_id for w in workflows if w.feature_id is not None}
        by_request = {w.request_id: w for w in workflows}
        items = []
        for request in s.scalars(select(DevelopmentRequest).order_by(DevelopmentRequest.created_at, DevelopmentRequest.request_id)):
            workflow = by_request.get(request.request_id)
            if workflow is not None:
                if view != 'all' and workflow.status in ('completed','cancelled'): continue
                if view == 'waiting' and workflow.status not in ('blocked','paused') and workflow.phase not in ('clarify','project_selection','prd_waiting','delivery_waiting'): continue
            elif view != 'all' and request.status not in ('clarifying','ready'):
                continue
            if workflow is not None and workflow.feature_id is not None:
                repository_id = s.scalar(select(Feature.repository_id).where(Feature.feature_id == workflow.feature_id))
                if repository_id not in self.allowed_repository_ids and not workflow.feature_id.startswith('workflow:'):
                    continue
            item = self._detail(s, request)
            text = item.pop('text')
            for field in ('artifacts','pending_decisions','stages'):item.pop(field,None)
            item.update(task_id=workflow.workflow_id if workflow else request.request_id,
                kind='workflow' if workflow else 'request', summary=text[:120],summary_truncated=len(text)>120)
            if workflow is not None:
                item.update(status=workflow.status, phase=workflow.phase, version=workflow.version, feature_id=workflow.feature_id)
            items.append(item)
        # Exclude owned Features even when their completed workflow was filtered
        # out above; otherwise an accepted task reappears as legacy intake.
        for feature in s.scalars(select(Feature).order_by(Feature.created_at,Feature.feature_id)):
            if feature.feature_id in owned_features or feature.repository_id not in self.allowed_repository_ids: continue
            if view != 'all' and feature.state in ('completed','cancelled'): continue
            if view == 'waiting' and not (feature.state.startswith('blocked_') or feature.state in ('needs_human','paused','awaiting_plan_review','awaiting_merge','reconciliation_required')): continue
            items.append(dict(task_id=feature.feature_id,feature_id=feature.feature_id,kind='legacy',
                version=feature.version,status=feature.state,phase=feature.state,
                summary='Legacy development task',summary_truncated=False,created_at=feature.created_at.isoformat(),
                execution_started=None))
        return sorted(items,key=lambda item:(item['created_at'],item['task_id']))

    def _cursor(self, snapshot, offset):
        raw = canonical_json(dict(snapshot_id=snapshot.snapshot_id, subject=snapshot.subject,
            view=snapshot.view, offset=offset, policy_digest=digest(sorted(self.allowed_repository_ids)))).encode()
        encoded = base64.urlsafe_b64encode(raw).rstrip(b'=')
        signature = hmac.new(self.cursor_key, b'dal.timeline-cursor/1.0\0'+encoded, hashlib.sha256).hexdigest()
        return encoded.decode()+'.'+signature

    def _parse_cursor(self, cursor, subject, view):
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError
            encoded, sig = cursor.split('.')
            expected = hmac.new(self.cursor_key, b'dal.timeline-cursor/1.0\0'+encoded.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, sig): raise ValueError
            value = json.loads(base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)))
            if set(value) != {'snapshot_id','subject','view','offset','policy_digest'} or value['subject'] != subject or value['view'] != view:
                raise ValueError
            if value['policy_digest'] != digest(sorted(self.allowed_repository_ids)): raise ValueError
            if type(value['offset']) is not int or value['offset'] < 0: raise ValueError
            return value
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise TimelineRefusal('CURSOR_INVALID') from None

    def list_tasks(self, *, subject, view='ongoing', limit=50, cursor=None):
        valid_id(subject)
        if view not in ('ongoing','all','waiting') or type(limit) is not int or not 1 <= limit <= 50:
            raise TimelineRefusal('INVALID_ARGUMENT')
        parsed = self._parse_cursor(cursor, subject, view) if cursor is not None else None
        def work(s):
            if parsed is None:
                items = self._task_items(s, view)
                snapshot_id, now = new_id(), self.now()
                snapshot = DevelopmentQuerySnapshot(snapshot_id=snapshot_id, subject=subject, view=view,
                    created_at=now, expires_at=now+timedelta(minutes=15),
                    sealed_body=self._seal(DevelopmentQuerySnapshot, snapshot_id, 'sealed_body', items))
                s.add(snapshot)
                offset = 0
            else:
                snapshot = s.scalar(select(DevelopmentQuerySnapshot).where(
                    DevelopmentQuerySnapshot.snapshot_id == parsed['snapshot_id']))
                if snapshot is None or snapshot.expires_at <= self.now():
                    raise TimelineRefusal('CURSOR_EXPIRED')
                if snapshot.subject != subject or snapshot.view != view:
                    raise TimelineRefusal('CURSOR_INVALID')
                items = self._open(DevelopmentQuerySnapshot, snapshot.snapshot_id, 'sealed_body', snapshot.sealed_body)
                offset = parsed['offset']
                if offset > len(items): raise TimelineRefusal('CURSOR_INVALID')
            stop = min(offset+limit, len(items))
            return dict(schema_version='dal.timeline/1.0', items=items[offset:stop], total=len(items),
                snapshot=snapshot.snapshot_id, as_of=snapshot.created_at.isoformat(), complete=stop==len(items),
                next_cursor=self._cursor(snapshot, stop) if stop < len(items) else None)
        with self.sessions() as s:
            return run_write_transaction(s, lambda: work(s))
