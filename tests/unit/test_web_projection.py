import copy
from personal_agent.runtime.web_projection import model_results


def test_web_projection_preserves_all_sources_and_original_evidence():
    sources = [{'kind': 'web_source', 'ref': f'web_{i}', 'source_ref': f'web_{i}',
                'provider_request_id': 'receipt', 'url': f'https://example.org/{i}',
                'title': 't'*300, 'snippet': 's'*2000, 'content': 'c'*8000,
                'content_present': True, 'truncated': False} for i in range(5)]
    original = [{'sources': sources}, {'exact_finance_total': '123456.78'}]
    before = copy.deepcopy(original)
    output = model_results(original)
    assert original == before
    assert len(output[0]['sources']) == 5
    for old, new in zip(sources, output[0]['sources']):
        assert new['source_ref'] == old['ref']
        assert new['url'] == old['url']
        assert new['truncated'] and new['content_present']
        assert len(new['content']) == len(new['snippet']) == 96
        assert len(new['title']) == 64
    assert output[1] == original[1]


def test_short_excerpt_preserves_existing_truncation():
    result = [{'sources': [{'source_ref': 'web_a', 'url': 'https://example.org/',
                          'content': None, 'snippet': 'short', 'truncated': True}]}]
    assert model_results(result)[0]['sources'][0]['truncated']


def test_excerpt_budget_counts_utf8_bytes_without_broken_characters():
    output = model_results([{'sources': [{'source_ref': 'web_cn', 'url': 'https://example.org/',
                'title': '文'*300, 'snippet': '摘'*300, 'content': '章'*300}]}])[0]['sources'][0]
    assert len(output['title'].encode()) <= 64
    assert len(output['snippet'].encode()) <= 96
    assert len(output['content'].encode()) <= 96
    assert '\ufffd' not in ''.join(str(v) for v in output.values())
    assert output['truncated']
