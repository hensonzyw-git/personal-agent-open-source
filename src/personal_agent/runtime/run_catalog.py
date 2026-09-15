"""Model-facing wrappers around one governed business catalog."""
from personal_agent.runtime.run_tools import RunToolSpec
from personal_agent_core.tool_ir import QUERY_EXPENSES


def obj(properties, required=None):
    return {'type':'object','properties':properties,'required':list(properties) if required is None else required,'additionalProperties':False}


STR={'type':'string','maxLength':8000}
REFS={'type':'array','items':{'type':'string'},'minItems':1,'maxItems':32}
CONSTRAINT=obj({'key':STR,'value':{},'source_refs':REFS})
FILTERS=obj({k:v for k,v in QUERY_EXPENSES.model_input_schema['properties'].items() if k not in {'view','cursor'}}, [])
# A category_total metric also names the selected by_category bucket.
FILTERS['properties']['category'] = QUERY_EXPENSES.model_input_schema['properties']['categories']['items']
TASK_VALIDATION=obj({'task_ref':{'type':'string'},'goal':STR,'source_refs':REFS,
    'constraints':{'type':'array','items':CONSTRAINT,'maxItems':32},
    'comparisons':{'type':'array','maxItems':8,'items':obj({'comparison_ref':STR,'metric_kind':{'enum':['total','count','category_total']},'current':FILTERS,'baseline':FILTERS,'source_refs':REFS}, ['metric_kind','current','baseline','source_refs'])}},
    ['goal','source_refs','constraints'])
TASK_VALIDATION['properties']['query_requirement'] = obj({
    'domain': {'const':'finance'}, 'result_kind': {'enum':['trip_breakdown','trip_total']},
    'trip_tag': QUERY_EXPENSES.model_input_schema['properties']['trip_tag'],
    'date_range': QUERY_EXPENSES.model_input_schema['properties']['date_range'], 'source_refs':REFS})
# The shared metadata shape is described once in the core instruction. Host
# validates TASK_VALIDATION before admitting the whole batch; provider-side
# duplication of it on every tool wastes the conservative byte budget.
TASK={'type':'object','required':['goal','source_refs','constraints']}
NODE={'oneOf':[obj({'kind':{'const':'metric'},'metric_ref':STR}),
    obj({'kind':{'const':'comparison'},'current_metric_ref':STR,'baseline_metric_ref':STR,'comparison_ref':STR}),
    obj({'kind':{'const':'web_claim'},'text':STR,'source_refs':REFS})]}
ANSWER={'oneOf':[obj({'kind':{'enum':['conversation','clarification','limitation']},'text':STR,
    'coverage':{'enum':['complete','partial']},'evidence_refs':{'type':'array','items':STR,'maxItems':32},'remaining_question':STR},['kind','text','coverage','evidence_refs']),
    obj({'kind':{'const':'analysis'},'analysis_nodes':{'type':'array','items':NODE,'minItems':1,'maxItems':16},
    'commentary':STR,'coverage':{'enum':['complete','partial']},'evidence_refs':{'type':'array','items':STR,'maxItems':32},'remaining_question':STR},['kind','analysis_nodes','commentary','coverage','evidence_refs'])]}


def _compact_schema(value, *, strip_defaults=False):
    # Business instructions are versioned once in domain_rules, not repeated
    # inside each model-facing parameter schema. Query defaults are optional
    # annotations, not instructions to repeat filters on cursor continuations.
    if isinstance(value,dict):
        return {k:({name:_compact_schema(schema, strip_defaults=strip_defaults) for name,schema in v.items()} if k in {'properties','$defs','definitions','patternProperties'} else _compact_schema(v, strip_defaults=strip_defaults))
                for k,v in value.items() if k not in ({'$schema','title','description','default'} if strip_defaults else {'$schema','title','description'})}
    if isinstance(value,list):return [_compact_schema(x, strip_defaults=strip_defaults) for x in value]
    return value


def catalog(declarations, *, trip_enabled=False):
    specs=[]
    for d in declarations:
        f=d['function']
        alias=f['name']
        if alias.startswith('agent.'): continue
        from personal_agent_core.tool_ir import contract_by_name
        read=contract_by_name(alias).effect=='read'
        arguments = _compact_schema(f['parameters'], strip_defaults=alias=='finance.query_expenses')
        if alias == 'finance.query_expenses':
            if not trip_enabled:
                arguments.get('properties', {}).pop('trip_tag', None)
                if 'enum' in arguments.get('properties', {}).get('view', {}):
                    arguments['properties']['view']['enum'] = [v for v in arguments['properties']['view']['enum'] if v != 'by_trip']
            # Mirror the MCP cursor contract without changing device grants.
            # The original schema already forbids other property names.
            arguments = {**arguments, 'anyOf': [
                {'properties': {'cursor': {'type': 'null'}}},
                {'maxProperties': 2, 'properties': {
                    'view': {'const': 'records'}, 'cursor': {'type': 'string', 'minLength': 1}}},
            ]}
        fields={'arguments':arguments,'task':TASK}
        if read and alias in {'finance.query_expenses','calendar.query_events'}:
            fields['response_mode']={'enum':['card','analyze']}
        if not read:
            fields['write_source_refs']=REFS
        specs.append(RunToolSpec(alias.replace('.','_'),alias,'read' if read else 'write',f['description'],obj(fields,['arguments','task'])))
    calendar=next((s for s in specs if s.business_name=='calendar.create_event'),None)
    if calendar:
        from dataclasses import replace
        original=calendar.schema['properties']['arguments']
        schema={'type':'object','additionalProperties':False,'required':['task'],'$defs':{'event':original},
            'properties':{'task':TASK,'write_source_refs':REFS,'arguments':{'$ref':'#/$defs/event'},
                'items':{'type':'array','minItems':2,'maxItems':8,'items':{'$ref':'#/$defs/event'}}},
            'oneOf':[{'required':['arguments'],'not':{'required':['items']}},{'required':['items'],'not':{'required':['arguments']}}]}
        specs[specs.index(calendar)]=replace(calendar,schema=schema,description=calendar.description+' 多个新建日程用 items 一次冻结；单个用 arguments。')
    specs.extend([
        RunToolSpec('agent_finish','agent.finish','control','完成交流、分析或提出必要澄清。普通交流允许自然语言。',obj({'answer':ANSWER,'task':TASK})),
        RunToolSpec('agent_list_pending_tasks','agent.list_pending_tasks','read','发现更多本 Timeline 未完成任务，计入读取额度。',obj({'cursor':{'type':'integer','minimum':0,'maximum':1000}},[])),
        RunToolSpec('agent_task_control','agent.task_control','control','独占取消、暂停或更正旧任务；来源必须为当前用户消息。',obj({'task_ref':STR,'action':{'enum':['cancel','pause','amend']},'source_refs':REFS,'replacement':TASK},['task_ref','action','source_refs']))])
    return specs
