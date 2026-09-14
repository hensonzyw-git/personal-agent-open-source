import json
from pathlib import Path
from personal_agent.runtime.answers import EvidenceCatalog


def test_shared_vectors_are_host_rendered():
    data=json.loads(Path('src/personal_agent/api/vectors/result_envelope_v2.json').read_text())
    assert data['synthetic'] is True
    for case in data['cases']:
        c=EvidenceCatalog()
        if case['name']=='comparison':
            current={'date_range':{'start':'2026-09-01','end':'2026-09-30'},'categories':[],
                     'name_contains':[],'is_family_expense':'all','personal_amount_cny':None}
            baseline={**current,'date_range':{'start':'2026-08-01','end':'2026-08-31'}}
            c.metric('q-current','total','120','元','CNY',current)
            c.metric('q-baseline','total','100','元','CNY',baseline)
            c.comparisons['comparison']={'metric_kind':'total','current':current,'baseline':baseline}
            assert case['response']['analysis_nodes'][0]['difference_decimal']=='20'
        assert c.answer(case['request'])==case['response']
