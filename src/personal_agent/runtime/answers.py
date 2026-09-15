"""Trusted evidence references and deterministic analysis rendering."""
from dataclasses import dataclass, asdict
import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json


class AnswerError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _comparison_filter_json(requested):
    """Expand only approved request defaults; never fill missing evidence.

    The frozen metadata, comparison identity and provider arguments stay intact.
    Explicit values and unknown keys are retained for the exact scope check.
    """
    from personal_agent_core.tool_ir import QUERY_EXPENSES

    if not isinstance(requested, dict):
        raise AnswerError('evidence_scope_mismatch')
    filters = copy.deepcopy(requested)
    properties = QUERY_EXPENSES.model_input_schema['properties']
    for key, schema in properties.items():
        if key not in {'view', 'cursor'} and 'default' in schema:
            filters.setdefault(key, copy.deepcopy(schema['default']))
    amount = filters.get('personal_amount_cny')
    if isinstance(amount, dict):
        for key, schema in properties['personal_amount_cny']['properties'].items():
            if 'default' in schema:
                amount.setdefault(key, copy.deepcopy(schema['default']))
    return canonical(filters)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value)-set(required)-set(optional) or not set(required)<=set(value):
        raise AnswerError('invalid_answer_shape')


def _text(value, limit=8000):
    if not isinstance(value, str) or len(value)>limit:
        raise AnswerError('invalid_answer_text')
    return value


@dataclass(frozen=True)
class EvidenceMetric:
    metric_ref: str
    evidence_ref: str
    metric_kind: str
    value_decimal: str
    unit: str
    currency: str | None
    filters_json: str
    data_status: str


class EvidenceCatalog:
    def __init__(self):
        self.metrics = {}
        self.evidence = {}
        self.comparisons = {}

    def metric(self, evidence_ref, kind, value, unit, currency, filters, status='complete'):
        try:
            number = Decimal(str(value))
            if not number.is_finite(): raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise AnswerError('invalid_metric') from None
        if kind not in {'total','count','category_total'} or unit not in {'元','条'} or (unit=='元' and currency!='CNY'):
            raise AnswerError('unknown_metric_unit')
        if kind=='count' and (number<0 or number!=number.to_integral_value() or unit!='条' or currency is not None):
            raise AnswerError('invalid_count')
        if kind!='count' and unit!='元':raise AnswerError('unknown_metric_unit')
        identity = [evidence_ref,kind,str(number),unit,currency,filters,status]
        ref = 'metric_' + hashlib.sha256(canonical(identity).encode()).hexdigest()
        self.metrics[ref] = EvidenceMetric(ref,evidence_ref,kind,str(number),unit,currency,canonical(filters),status)
        self.evidence.setdefault(evidence_ref, {'kind':'query_card','ref':evidence_ref})
        return ref

    def add_query(self, ref, projection, *, tool='finance.query_expenses'):
        from personal_agent.api.finance_query_projection import FinanceQueryProjection
        card = projection.to_dict() if hasattr(projection,'to_dict') else projection
        self.evidence[ref] = {'kind':'query_card','ref':ref,'query_result':card,'tool':tool}
        if isinstance(projection, FinanceQueryProjection):
            # Legacy cards omit coverage; their completion still depends on pagination.
            status = 'partial' if projection.next_cursor or (projection.coverage or {}).get('scope_coverage', 'complete') != 'complete' else 'complete'
            if projection.view in {'total','by_category','by_trip'}:
                if projection.personal_spend_total_cny is not None:self.metric(ref,'total',projection.personal_spend_total_cny,'元','CNY',projection.filters_applied,status)
                for bucket in projection.by_category:
                    self.metric(ref,'category_total',bucket.personal_spend_total_cny,'元','CNY',{**projection.filters_applied,'category':bucket.category},status)
                self.metric(ref,'count',projection.record_count,'条',None,projection.filters_applied,status)
        return self.evidence[ref]

    def _metric(self, ref):
        if ref not in self.metrics: raise AnswerError('unknown_metric')
        return self.metrics[ref]

    def _sentence(self, m):
        return f'{m.filters_json}：{m.value_decimal} {m.unit}'

    def node(self, node):
        if not isinstance(node,dict): raise AnswerError('invalid_analysis_node')
        kind=node.get('kind')
        if kind=='metric':
            _keys(node,('kind','metric_ref'))
            m=self._metric(node['metric_ref'])
            return {**node,'text':self._sentence(m),'metric':asdict(m)}
        if kind=='comparison':
            _keys(node,('kind','current_metric_ref','baseline_metric_ref','comparison_ref'))
            a,b=self._metric(node['current_metric_ref']),self._metric(node['baseline_metric_ref'])
            req=self.comparisons.get(node['comparison_ref'])
            if (not req or a.filters_json!=_comparison_filter_json(req['current']) or b.filters_json!=_comparison_filter_json(req['baseline']) or
                a.metric_kind!=b.metric_kind or a.metric_kind!=req['metric_kind'] or
                (a.unit,a.currency)!=(b.unit,b.currency) or a.data_status!='complete' or b.data_status!='complete'):
                raise AnswerError('evidence_scope_mismatch')
            delta=Decimal(a.value_decimal)-Decimal(b.value_decimal)
            direction='增加' if delta>0 else '减少' if delta<0 else '持平'
            return {**node,'text':f'{self._sentence(a)}；基准 {self._sentence(b)}；{direction} {abs(delta)} {a.unit}',
                'difference_decimal':str(delta),'current':asdict(a),'baseline':asdict(b)}
        if kind=='web_claim':
            _keys(node,('kind','text','source_refs'))
            _text(node['text'])
            refs=node['source_refs']
            if not isinstance(refs,list) or not refs or len(refs)>5 or any(not isinstance(r,str) or self.evidence.get(r,{}).get('kind')!='web_source' for r in refs):
                raise AnswerError('unknown_web_source')
            return {**node,'sources':[self.evidence[r] for r in refs]}
        raise AnswerError('invalid_analysis_node')

    def answer(self, answer):
        kind=answer.get('kind')
        if kind not in {'conversation','analysis','clarification','limitation'}: raise AnswerError('invalid_answer_kind')
        common=('kind','coverage','evidence_refs')
        _keys(answer,common+(('analysis_nodes','commentary') if kind=='analysis' else ('text',)),('remaining_question',))
        refs=answer['evidence_refs']
        if not isinstance(refs,list) or len(refs)>32 or any(not isinstance(r,str) or r not in self.evidence for r in refs):
            raise AnswerError('unknown_evidence')
        if answer['coverage'] not in {'complete','partial'}: raise AnswerError('invalid_coverage')
        result={'version':2,'kind':kind,'task_status':'waiting' if kind=='clarification' else 'completed',
            'coverage':answer['coverage'],'evidence':list(self.evidence.values())}
        if kind=='analysis':
            nodes=answer['analysis_nodes']
            if not isinstance(nodes,list) or not 1<=len(nodes)<=16: raise AnswerError('invalid_analysis_nodes')
            rendered=[self.node(n) for n in nodes]
            used=[]
            for node in rendered:
                for key in ('metric','current','baseline'):
                    if key in node:
                        metric=node[key];used.append(metric['evidence_ref'])
                        if answer['coverage']=='complete' and metric['data_status']!='complete':raise AnswerError('partial_metric')
                used.extend(node.get('source_refs',[]))
            if not set(used)<=set(refs):raise AnswerError('missing_evidence_ref')
            result.update(analysis_nodes=rendered,commentary=_text(answer['commentary']),text='\n'.join(n['text'] for n in rendered))
        else: result['text']=_text(answer['text'])
        if 'remaining_question' in answer: result['remaining_question']=_text(answer['remaining_question'])
        return result
