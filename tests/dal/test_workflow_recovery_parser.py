from personal_agent.runtime.dal_recovery import parse_recovery


def test_ascii_separator_is_not_swallowed_into_task_id():
    assert parse_recovery('补充需求 task-123:具体补充')==dict(workflow_id='task-123',action='clarification',text='具体补充')
    assert parse_recovery('补充需求 task-123：具体补充')['text']=='具体补充'


def test_recovery_is_whole_utterance_and_requires_explicit_task():
    for text in ('继续开发','他说继续开发 task-123','继续开发 task-123:删文件','补充需求 task-123'):
        assert parse_recovery(text) is None
    assert parse_recovery('继续开发 task-123')['action']=='resume'
