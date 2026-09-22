"""Same frozen wire vectors as Swift; no copied protocol examples."""
import json
from pathlib import Path
import pytest
from sqlalchemy import select
from personal_agent_dal.timeline.requests import digest
from personal_agent.storage.models import DalTimelineCommand
from tests.integration.test_dal_timeline_bridge import bridge_world
from tests.dal.test_timeline_requests import world
from tests.integration.test_dal_authorization_command_status import queue

VECTOR=json.loads((Path(__file__).parents[1]/'fixtures/dal_phone_authorization.synthetic.json').read_text())


def test_shared_canonical_authorization_digest():
    assert digest(VECTOR['canonical_body'])==VECTOR['canonical_sha256']


@pytest.mark.parametrize('case',VECTOR['command_states'])
def test_server_matches_shared_command_state_vector(bridge_world,case):
    bridge,auth,*_=bridge_world
    queue(bridge,auth)
    wire=case['wire']
    with bridge.sessions() as s,s.begin():
        row=s.scalar(select(DalTimelineCommand))
        row.status=wire['status'];row.attempts=case['attempts'];row.delivery_error=wire['delivery_error']
        if wire['receipt']:row.sealed_receipt=bridge._seal(row.command_id,'sealed_receipt',wire['receipt'])
    assert bridge.command_status(auth,'command')==wire
