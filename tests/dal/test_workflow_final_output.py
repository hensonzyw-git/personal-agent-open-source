"""Native Codex progress is distinct from its authoritative last-message file."""
import json
from pathlib import Path
import pytest
from personal_agent_dal.worker.role_adapter import parse_events, read_final_report
from personal_agent_dal.worker.supervisor import SupervisorRefusal


def stream(*messages):
    return b''.join((json.dumps(e)+'\n').encode() for e in [
        *[{'type':'item.completed','item':{'id':str(i),'type':'agent_message','text':text}} for i,text in enumerate(messages)],
        {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':20}},
    ])


def test_native_progress_and_exact_final_are_both_preserved():
    final='{"kind":"prd","text":"Synthetic"}'
    result=parse_events(stream('I will inspect the workspace.',final),'codex_cli',final_report=final)
    assert result['report']==final
    assert result['progress_messages']==['I will inspect the workspace.']


@pytest.mark.parametrize('final',['', '{"different":true}', ' {"ok":true}'])
def test_missing_or_mismatched_final_refused(final):
    with pytest.raises(SupervisorRefusal):
        parse_events(stream('{"ok":true}'),'codex_cli',final_report=final)


def test_invalid_stream_cannot_be_repaired_by_final_file():
    with pytest.raises(SupervisorRefusal):
        parse_events(stream('final')+b'bad\n','codex_cli',final_report='final')


def test_mixed_prose_final_is_not_repaired():
    final='Here is the result: {"ok":true}'
    result=parse_events(stream('Progress',final),'codex_cli',final_report=final)
    with pytest.raises(json.JSONDecodeError):json.loads(result['report'])


def test_legacy_report_keeps_all_messages():
    assert parse_events(stream('one','two'),'codex_cli')['report']=='one\ntwo'


@pytest.mark.parametrize('kind',['missing','symlink','hardlink','directory','oversize','invalid_utf8'])
def test_final_file_refuses_unsafe_shapes(tmp_path,kind):
    target=tmp_path/'final'
    if kind=='symlink':
        other=tmp_path/'other';other.write_text('final');target.symlink_to(other)
    elif kind=='hardlink':
        other=tmp_path/'other';other.write_text('final');target.hardlink_to(other)
    elif kind=='directory':target.mkdir()
    elif kind=='oversize':target.write_bytes(b'x'*131073)
    elif kind=='invalid_utf8':target.write_bytes(b'\xff')
    with pytest.raises(SupervisorRefusal):read_final_report(str(target))


def test_final_file_is_read_exactly(tmp_path):
    target=tmp_path/'final';target.write_text('{"ok":true}')
    assert read_final_report(str(target))=='{"ok":true}'


def test_final_paths_are_attempt_bound_and_outside_tool_write_roots(tmp_path):
    from personal_agent_dal.worker.role_adapter import codex_final_path,LaunchPlan
    from personal_agent_dal.worker.runtime_admission import plan_contract
    from dataclasses import replace
    one={'workspace':str(tmp_path/'work'),'temp':str(tmp_path/'tmp'/'one'),'git':str(tmp_path/'git')}
    two=dict(one,temp=str(tmp_path/'tmp'/'two'))
    a,b=codex_final_path(one),codex_final_path(two)
    assert a!=b and Path(a).parent==Path(one['git'])
    assert not Path(a).is_relative_to(Path(one['workspace']))
    assert not Path(a).is_relative_to(Path(one['temp']))
    p=LaunchPlan(('codex','--output-last-message',a),one['temp'],{},'codex_cli','a'*64,'synthetic',final_report_path=a)
    q=replace(p,argv=('codex','--output-last-message',b),cwd=two['temp'],final_report_path=b)
    assert plan_contract(p,one)==plan_contract(q,two)
    with pytest.raises(SupervisorRefusal):plan_contract(replace(p,final_report_path='/unbound'),one)


def telemetry(n=1):
    return {'type':'system','subtype':'thinking_tokens','estimated_tokens':n,
        'estimated_tokens_delta':1,'session_id':'synthetic','uuid':str(n)}


def test_claude_telemetry_does_not_consume_business_steps():
    events=[telemetry(n) for n in range(1,101)]
    events.append({'type':'result','result':'{"ok":true}','is_error':False})
    raw=b''.join((json.dumps(e)+'\n').encode() for e in events)
    assert parse_events(raw,'claude_code')['report']=='{"ok":true}'


@pytest.mark.parametrize('change',[{'estimated_tokens_delta':-1},{'estimated_tokens':True},{'unexpected':'value'}])
def test_malformed_telemetry_is_not_exempt(change):
    item=dict(telemetry(),**change)
    with pytest.raises(SupervisorRefusal):
        parse_events((json.dumps(item)+'\n'+json.dumps({'type':'result','result':'ok'})+'\n').encode(),'claude_code')


def test_nontelemetry_events_still_have_step_limit():
    raw=b''.join((json.dumps({'type':'system','subtype':'synthetic'})+'\n').encode() for _ in range(65))
    with pytest.raises(SupervisorRefusal,match='STEP_LIMIT'):parse_events(raw,'claude_code')


def test_streaming_counter_handles_fragmented_telemetry_and_limits():
    from personal_agent_dal.worker.role_adapter import CLIEventCounter
    raw=b''.join((json.dumps(telemetry(n))+'\n').encode() for n in range(1,101))
    counter=CLIEventCounter('claude_code',1)
    for i in range(0,len(raw),7):counter.feed(raw[i:i+7])
    assert (counter.events,counter.steps,counter.pending)==(100,0,b'')
    counter.feed(b'{"type":"result","result":"ok"}\n')
    with pytest.raises(SupervisorRefusal,match='STEP_LIMIT'):counter.feed(b'{"type":"system"}\n')


def test_streaming_malformed_telemetry_fails_closed():
    from personal_agent_dal.worker.role_adapter import CLIEventCounter
    c=CLIEventCounter('claude_code',64)
    with pytest.raises(SupervisorRefusal):c.feed(b'{"type":"system","subtype":"thinking_tokens"}\n')
