import json
from datetime import datetime,timezone,timedelta
import pytest
from personal_agent_dal.worker.workflow_prompt import build_prompt
from personal_agent_dal.worker.supervisor import SupervisorRefusal


def args():
    return dict(source_directory="/allocated/project with 'quote'",scratch_directory='/allocated/tmp/attempt',
        now=datetime(2026,9,19,4,0,tzinfo=timezone(timedelta(hours=8))))


def test_source_context_is_explicit_and_independent_of_untrusted_inputs():
    raw=build_prompt({'phase':'researching','runtime_context':{'source_directory':'/untrusted'}},**args()).decode()
    context=json.loads(raw.split('TRUSTED RUNTIME CONTEXT:\n',1)[1].split('\nTASK DATA:',1)[0])
    assert context=={'source_directory':"/allocated/project with 'quote'",'scratch_directory':'/allocated/tmp/attempt',
        'observed_at_utc':'2026-09-18T20:00:00+00:00'}
    assert '/untrusted' not in raw.split('\nTASK DATA:',1)[0]
    assert 'scratch contents are not project source' in raw


@pytest.mark.parametrize('key,value',[('source_directory','relative'),('scratch_directory','relative'),
    ('source_directory','/bad\x00path'),('now',datetime(2026,9,19))])
def test_invalid_runtime_context_refused(key,value):
    kw=args();kw[key]=value
    with pytest.raises(SupervisorRefusal):build_prompt({'phase':'researching'},**kw)
