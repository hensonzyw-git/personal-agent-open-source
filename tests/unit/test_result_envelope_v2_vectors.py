import json
from pathlib import Path
from personal_agent.runtime.answers import EvidenceCatalog


def test_shared_vectors_are_host_rendered():
    data=json.loads(Path('src/personal_agent/api/vectors/result_envelope_v2.json').read_text())
    assert data['synthetic'] is True
    for case in data['cases']:
        c=EvidenceCatalog()
        if case['name']=='comparison':
            c.metric('q-current','total','120','元','CNY',{'month':'2026-09'})
            c.metric('q-baseline','total','100','元','CNY',{'month':'2026-08'})
            c.comparisons['comparison']={'metric_kind':'total','current':{'month':'2026-09'},'baseline':{'month':'2026-08'}}
        assert c.answer(case['request'])==case['response']
