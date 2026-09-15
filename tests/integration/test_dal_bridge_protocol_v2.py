"""Persisted claims are immutable across delivery, upgrade and lost replies."""
import json
from datetime import datetime, timezone

import jwt
import pytest
from sqlalchemy import select, func
from personal_agent.storage.engine import session_factory
from personal_agent.storage.models import DalResumeDecision, DalResumeDelivery
from tests.dal.test_p0_02_review_regressions import world
from tests.integration.test_dal_resume_bridge import bridge_world, click
from tests.dal.test_resume_protocol_v2 import seed_historical


@pytest.mark.parametrize('legacy,imported,expire', [(False,False,False), (False,False,True),
    (True,False,True), (True,True,False), (True,True,True)])
def test_original_outbox_survives_retries(bridge_world, world, legacy, imported, expire):
    engine, bridge, auth, p, transport = bridge_world
    result = bridge.decide(auth, click(p))
    decision_id = result['decision_id']
    with session_factory(engine)() as s, s.begin():
        row = s.get(DalResumeDecision, decision_id)
        claims = json.loads(row.claims)
        assert claims['proposal_id'] == p['proposal_id'] == row.proposal_id
        if legacy:
            claims.pop('proposal_id')
            # Explicit pre-upgrade outbox fixture, including original whitespace.
            row.claims = json.dumps(claims, indent=2)
        original = row.claims
    if imported: seed_historical(world, p, claims)
    engine.dispose()
    with engine.connect() as c:
        assert c.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    sent = []
    deliver = transport.deliver
    def capture(assertion):
        sent.append(jwt.decode(assertion, bridge.key.public_key(), algorithms=['ES256'],
            options={'verify_aud':False}))
        return deliver(assertion)
    transport.deliver = capture
    bridge.deliver_pending()
    assert bridge.decide(auth, click(p))['decision_id'] == decision_id
    transport.lose_response = False
    if expire:
        bridge.now = lambda: datetime.fromtimestamp(claims['exp'], timezone.utc)
    bridge.deliver_pending()
    with session_factory(engine)() as s:
        assert s.get(DalResumeDecision, decision_id).claims == original
        row = s.get(DalResumeDelivery, decision_id)
        assert row.status == ('delivery_unknown' if expire else 'accepted')
        assert row.attempts == (1 if expire else 2)
        assert s.scalar(select(func.count()).select_from(DalResumeDecision)) == 1
    assert sent == [claims] * (1 if expire else 2)
