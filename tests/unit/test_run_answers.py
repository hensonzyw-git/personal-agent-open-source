import pytest
from personal_agent.runtime.answers import EvidenceCatalog, AnswerError


def test_comparison_labels_and_numbers_are_rendered_by_host():
    c = EvidenceCatalog()
    a = c.metric('q1', 'total', '120', '元', 'CNY', {'month':'2026-09'})
    b = c.metric('q2', 'total', '100', '元', 'CNY', {'month':'2026-08'})
    c.comparisons['cmp'] = {'metric_kind':'total', 'current':{'month':'2026-09'}, 'baseline':{'month':'2026-08'}}
    node = {'kind':'comparison','current_metric_ref':a,'baseline_metric_ref':b,'comparison_ref':'cmp'}
    out = c.answer({'kind':'analysis','coverage':'complete','evidence_refs':['q1','q2'],'analysis_nodes':[node],'commentary':''})
    assert '增加 20 元' in out['text']
    node['current_metric_ref'],node['baseline_metric_ref']=b,a
    with pytest.raises(AnswerError): c.answer({'kind':'analysis','coverage':'complete','evidence_refs':['q1','q2'],'analysis_nodes':[node],'commentary':''})


@pytest.mark.parametrize('bad', [
 {'kind':'metric','metric_ref':'unknown'},
 {'kind':'metric','metric_ref':'known','value':'99'},
 {'kind':'web_claim','text':'claim','source_refs':['unknown']},
])
def test_analysis_rejects_untrusted_facts(bad):
    with pytest.raises(AnswerError):
        EvidenceCatalog().answer({'kind':'analysis','coverage':'complete','evidence_refs':[], 'analysis_nodes':[bad],'commentary':''})
