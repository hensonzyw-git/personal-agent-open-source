import asyncio
import httpx
import pytest
from personal_agent.search.policy import public_url,SearchError
from personal_agent.search.adapter import SearchAdapter,SearchConfig


@pytest.mark.parametrize('url',['http://127.0.0.1/','https://example.com:123/','https://user@example.com/','https://example.com/?token=secret','https://example.com/private/x','http://metadata.google.internal/'])
def test_reject_private_url(url):
    with pytest.raises(SearchError):public_url(url,resolve=lambda h:['8.8.8.8'])


def test_mixed_dns_rejected():
    with pytest.raises(SearchError):public_url('https://example.com/',resolve=lambda h:['8.8.8.8','10.0.0.1'])


def test_402_body_never_read_or_reused(caplog):
    calls=[]
    def transport(req):
        calls.append(req)
        return httpx.Response(402,content=b'password=SYNTHETIC_PRIVATE')
    adapter=SearchAdapter(SearchConfig(enabled=True,auth_mode='anonymous'),transport=httpx.MockTransport(transport))
    reserved=[]
    with pytest.raises(SearchError,match='search_quota'):
        asyncio.run(adapter.call('search.web',{'query':'public weather'},timeout=5,reserve=lambda:reserved.append(1)))
    assert len(calls)==len(reserved)==1 and 'Authorization' not in calls[0].headers
    assert 'SYNTHETIC_PRIVATE' not in caplog.text


def test_success_pinned_endpoint_and_receipt():
    from uuid import uuid4
    rid=str(uuid4());calls=[]
    def transport(request):
        calls.append(request)
        return httpx.Response(200,json={'code':0,'request_id':rid,'data':{'results':[{'title':'Public','url':'https://example.org/','snippet':'Public fact'}]}},headers={'x-request-id':rid})
    adapter=SearchAdapter(SearchConfig(enabled=True,auth_mode='key'),key='synthetic',transport=httpx.MockTransport(transport),resolve=lambda h:['8.8.8.8'])
    result=asyncio.run(adapter.call('search.web',{'query':'public fact'},timeout=5,reserve=lambda:None))
    assert result[0]['provider_request_id']==rid
    assert str(calls[0].url)=='https://api.anysearch.com/v1/search'
    assert calls[0].headers['Authorization']=='Bearer synthetic'


@pytest.mark.parametrize('status,body,header',[
    (302,{},{}),(200,{'code':0,'request_id':'not-a-receipt','data':{}},{}),
    (200,{'code':0,'request_id':'11111111-1111-4111-8111-111111111111','data':{'results':[]}}, {'x-request-id':'22222222-2222-4222-8222-222222222222'}),
    (200,{'code':0,'request_id':'11111111-1111-4111-8111-111111111111','data':{'results':[{'title':'bad','url':'http://127.0.0.1/'}]}},{}),
])
def test_provider_failure_shapes_are_bounded(status,body,header):
    calls=[];reserved=[]
    def transport(request):calls.append(request);return httpx.Response(status,json=body,headers=header)
    a=SearchAdapter(SearchConfig(enabled=True,auth_mode='anonymous'),transport=httpx.MockTransport(transport),resolve=lambda h:['8.8.8.8'])
    with pytest.raises(SearchError):asyncio.run(a.call('search.web',{'query':'public'},timeout=5,reserve=lambda:reserved.append(1)))
    assert len(calls)==len(reserved)==1


def test_extract_sends_only_pinned_url_and_preserves_body():
    from uuid import uuid4
    rid=str(uuid4());requests=[]
    def transport(request):
        requests.append(request)
        return httpx.Response(200,json={'code':0,'request_id':rid,'data':{'title':'Public','url':'https://example.org/','content':'x'*9000}})
    a=SearchAdapter(SearchConfig(enabled=True,extract_enabled=True,auth_mode='anonymous'),transport=httpx.MockTransport(transport),resolve=lambda h:['8.8.8.8'])
    result=asyncio.run(a.call('search.read_page',{'public_url':'https://example.org/'},timeout=5,reserve=lambda:None))
    import json
    assert json.loads(requests[0].content)=={'url':'https://example.org/'}
    assert str(requests[0].url)=='https://api.anysearch.com/v1/extract'
    assert not result[0]['truncated'] and len(result[0]['content'])==9000
