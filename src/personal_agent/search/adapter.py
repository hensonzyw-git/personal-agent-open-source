"""Pinned AnySearch HTTP API; no anonymous fallback, retries or error bodies."""
from dataclasses import dataclass
from uuid import UUID
import hashlib
import httpx
from personal_agent.runtime.response_witness import strict_json
from personal_agent.search.policy import SearchError, public_url, scan_public_text


@dataclass(frozen=True)
class SearchConfig:
    enabled: bool = False
    extract_enabled: bool = False
    auth_mode: str | None = None
    daily_limit: int = 100


class SearchAdapter:
    def __init__(self, config, *, key=None, transport=None, resolve=None):
        self.config,self.key,self.transport,self.resolve=config,key,transport,resolve

    async def call(self, tool, args, *, timeout, reserve):
        c=self.config
        if not c.enabled or c.auth_mode not in {'anonymous','key'} or (c.auth_mode=='key' and not self.key):
            raise SearchError('search_disabled')
        if tool=='search.web':
            if set(args)-{'query','language','max_results'}: raise SearchError('invalid_search_args')
            query=args.get('query'); scan_public_text(query)
            n=args.get('max_results',5)
            if len(query)>512 or type(n) is not int or not 1<=n<=5: raise SearchError('invalid_search_args')
            body={'query':query,'max_results':n}
            if 'language' in args:
                if not isinstance(args['language'],str) or len(args['language'])>32: raise SearchError('invalid_search_args')
                scan_public_text(args['language'])
                body['language']=args['language']
            path,limit='/v1/search',512*1024
        elif tool=='search.read_page':
            if not c.extract_enabled: raise SearchError('extract_disabled')
            if set(args)!={'public_url'}: raise SearchError('invalid_extract_args')
            body={'url':public_url(args['public_url'],resolve=self.resolve)}
            path,limit='/v1/extract',1024*1024
        else: raise SearchError('unknown_search_tool')
        if not 0<timeout<=10: raise SearchError('search_timeout')
        reserve()  # Atomically commit run/task/day reservations before HTTP.
        headers={'Accept-Encoding':'identity'}
        if c.auth_mode=='key': headers['Authorization']='Bearer '+self.key
        try:
            async with httpx.AsyncClient(transport=self.transport,trust_env=False,follow_redirects=False,timeout=timeout) as client:
                async with client.stream('POST','https://api.anysearch.com'+path,json=body,headers=headers) as response:
                    if response.status_code!=200:
                        raise SearchError({400:'invalid_query',401:'search_unauthorized',402:'search_quota',403:'search_forbidden',429:'search_rate_limit'}.get(response.status_code,'search_provider_failed'))
                    if response.headers.get('content-encoding','identity')!='identity': raise SearchError('compressed_response')
                    raw=bytearray()
                    async def chunks():
                        if response.is_stream_consumed:
                            yield response.content
                        else:
                            async for chunk in response.aiter_raw(): yield chunk
                    async for chunk in chunks():
                        raw.extend(chunk)
                        if len(raw)>limit: raise SearchError('content_too_large')
                    data=strict_json(bytes(raw))
                    rid=data['request_id']
                    if UUID(rid).version!=4 or type(data['code']) is not int or data['code']!=0 or response.headers.get('x-request-id',rid)!=rid:
                        raise SearchError('invalid_search_response')
                    entries=data['data']['results'] if tool=='search.web' else [data['data']]
                    if not isinstance(entries,list) or len(entries)>body.get('max_results',1): raise SearchError('invalid_search_response')
                    result=[]
                    for e in entries:
                        url=public_url(e['url'],resolve=self.resolve)
                        if tool=='search.read_page' and url!=body['url']: raise SearchError('extract_url_mismatch')
                        title=e['title']; content=e.get('content'); snippet=e.get('snippet','')
                        if not isinstance(title,str) or not isinstance(snippet,str) or (content is not None and not isinstance(content,str)): raise SearchError('invalid_search_response')
                        ref='web_'+hashlib.sha256((rid+url).encode()).hexdigest()
                        result.append({'kind':'web_source','ref':ref,'source_ref':ref,'title':title[:512],'url':url,
                            'snippet':snippet[:2000],'content':content[:8000] if content is not None else None,
                            'content_present':content is not None,'truncated':bool(content and len(content)>8000),'provider_request_id':rid})
                    return result
        except SearchError: raise
        except Exception:
            raise SearchError('search_provider_failed') from None
