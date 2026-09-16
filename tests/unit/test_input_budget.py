import pytest
from personal_agent.runtime.input_budget import input_budget_from_env, InputBudget


def test_expanded_budget_requires_verified_artifact_and_window(tmp_path):
    env={'ADK_INPUT_TOKEN_LIMIT':'200000','MODEL_PROVIDER':'deepseek','MODEL_ID':'deepseek-flash'}
    with pytest.raises(ValueError,match='window'):input_budget_from_env(env)
    env['MODEL_CONTEXT_TOKENS']='1000000'
    with pytest.raises(ValueError,match='tokenizer_required'):input_budget_from_env(env)
    path=tmp_path/'tokenizer.json';path.write_text('{}');env['ADK_TOKENIZER_PATH']=str(path)
    with pytest.raises(ValueError,match='hash_mismatch'):input_budget_from_env(env)
    assert input_budget_from_env({}) is None


def test_actual_tokenizer_large_text_and_outbound_guard():
    import os
    path=os.environ.get('ADK_TEST_TOKENIZER_PATH')
    if not path:pytest.skip('official tokenizer artifact supplied by deployment validation')
    b=input_budget_from_env({'ADK_INPUT_TOKEN_LIMIT':'200000','MODEL_PROVIDER':'deepseek',
        'MODEL_ID':'deepseek-flash','MODEL_CONTEXT_TOKENS':'1000000','ADK_TOKENIZER_PATH':path})
    text=('中文正文🙂 and English. '*10000)+'尾部事实'
    assert len(text.encode())>200000
    assert b.total(text)<200000
    body={'model':b.model,'max_tokens':8192,'messages':[{'role':'user','content':text}]}
    assert b.check_request(body)<200000
    with pytest.raises(ValueError,match='capacity_exceeded'):b.check_request({**body,'messages':[{'role':'user','content':text*8}]})
    with pytest.raises(ValueError,match='output_limit'):b.check_request({**body,'max_tokens':8193})
    clipped=b.excerpt('汉字🙂'*1000,30)
    assert b.estimate(clipped)<=30 and '\ufffd' not in clipped


def test_final_sdk_request_is_blocked_before_transport():
    import asyncio
    import httpx
    from personal_agent.runtime.witnessed_model import WitnessedLiteLlm
    from personal_agent.runtime.response_witness import AttemptBinding,ResponseViolation
    class Budget:
        def check_request(self,body,image_tokens):
            raise ValueError('capacity_exceeded')
    model=WitnessedLiteLlm(model='openai/synthetic',provider_name='deepseek',api_key='synthetic',
                          binding=AttemptBinding('synthetic',1,1),input_budget=Budget())
    with pytest.raises(ResponseViolation,match='capacity_exceeded'):
        asyncio.run(model._check_input_budget(httpx.Request('POST','https://api.deepseek.com/chat/completions',json={'messages':[]})))



def test_real_tokenizer_rejects_sdk_request_without_network():
    import os, asyncio, httpx
    from dataclasses import replace
    from google.genai import types
    from google.adk.models.llm_request import LlmRequest
    from personal_agent.runtime.witnessed_model import WitnessedLiteLlm
    from personal_agent.runtime.response_witness import AttemptBinding,ResponseViolation
    path=os.environ.get('ADK_TEST_TOKENIZER_PATH')
    if not path:pytest.skip('official tokenizer required')
    budget=input_budget_from_env({'ADK_INPUT_TOKEN_LIMIT':'200000','MODEL_PROVIDER':'deepseek',
        'MODEL_ID':'deepseek-flash','MODEL_CONTEXT_TOKENS':'1000000','ADK_TOKENIZER_PATH':path})
    calls=[]
    model=WitnessedLiteLlm(model='openai/synthetic',provider_name='deepseek',api_key='synthetic',
        binding=AttemptBinding('synthetic',1,1),input_budget=replace(budget,model='synthetic'),
        transport=httpx.MockTransport(lambda r:calls.append(r)))
    request=LlmRequest(contents=[types.Content(role='user',parts=[types.Part(text='中文 mixed tokens '*100000)])],
        config=types.GenerateContentConfig(max_output_tokens=8192))
    async def run():
        return [r async for r in model.generate_content_async(request)]
    with pytest.raises(ResponseViolation,match='capacity_exceeded'):asyncio.run(run())
    assert calls==[]


def test_unknown_input_field_cannot_bypass_counting():
    class Characters:
        def encode(self,text,**kwargs):
            from types import SimpleNamespace
            return SimpleNamespace(ids=list(text))
    b=InputBudget(Characters(),'synthetic',limit=1200)
    body={'model':'synthetic','max_tokens':1,'messages':[]}
    assert b.check_request(body)<1200
    with pytest.raises(ValueError,match='capacity_exceeded'):
        b.check_request({**body,'functions':[{'description':'x'*2000}]})
    with pytest.raises(ValueError,match='unsupported_input_content'):
        b.check_request({**body,'messages':[{'role':'user','content':{'unrecognized':'x'}}]})
