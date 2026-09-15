"""Trip result requirements: source-bound metadata and factual completion checks."""
import copy
import re
from personal_agent.runtime.answers import AnswerError, canonical
from personal_agent.runtime.run_store import RunStateError

TRIP_PROMPT = '''旅行场次：按目的地/场次统计用finance.query_expenses view=by_trip；单场次总额用total+精确trip_tag，明细用records+trip_tag。场次编号及内部空格不合并，无日期且场次明确不追问、不默认今年。任务必须带query_requirement={domain:finance,result_kind:trip_breakdown或trip_total,trip_tag:标签或null,date_range:范围或null,source_refs:用户来源}。constraints保留相同筛选。每次调用/finish保留要求；不能用分类或明细结束聚合。简单汇总省略response_mode或用card，由Host直接返回；有后续分析时显式analyze；coverage有限时只称小计。'''


def explicit_breakdown(text):
    return bool(re.search(r'旅行|场次', text) and re.search(r'按(?:照)?目的地|按场次|各场次|各次旅行', text)
                and re.search(r'统计|聚合|分别|各花', text))


def freeze_requirement(metadata, old, text):
    result = copy.deepcopy(metadata)
    previous = (old or {}).get('query_requirement')
    supplied = result.get('query_requirement')
    if previous is not None:
        if supplied is not None and canonical(previous) != canonical(supplied):
            raise RunStateError('trip_requirement_change_needs_amend')
        result['query_requirement'] = copy.deepcopy(previous)
    if explicit_breakdown(text) and not result.get('query_requirement'):
        raise RunStateError('trip_requirement_missing')
    if explicit_breakdown(text) and result['query_requirement']['result_kind'] != 'trip_breakdown':
        raise RunStateError('trip_requirement_mismatch')
    return result


def matches_requirement(card, metadata):
    requirement = (metadata or {}).get('query_requirement')
    if requirement is None:
        return False
    q = card.get('query_result', {})
    expected = 'by_trip' if requirement['result_kind'] == 'trip_breakdown' else 'total'
    filters = q.get('filters_applied', {})
    return (card.get('tool') == 'finance.query_expenses' and q.get('view') == expected
            and q.get('coverage', {}).get('scan_complete') is True
            and filters.get('trip_tag') == requirement.get('trip_tag')
            and canonical(filters.get('date_range')) == canonical(requirement.get('date_range')))


def check_completion(answer, metadata):
    if not (metadata or {}).get('query_requirement'):
        return
    if answer['kind'] in {'limitation', 'clarification'}:
        return
    cards = [c for c in answer.get('evidence', []) if matches_requirement(c, metadata)]
    if not cards:
        raise AnswerError('trip_summary_required')
    from personal_agent.runtime.task_contracts import validate_evidence_scope
    for card in cards:
        validate_evidence_scope(card, metadata)
    if answer.get('coverage') == 'complete' and any(c['query_result']['coverage']['scope_coverage'] != 'complete' for c in cards):
        raise AnswerError('trip_coverage_incomplete')
