"""Host-owned task/evidence bindings. No language classification or provider calls."""
import copy
import hashlib

from personal_agent.runtime.answers import AnswerError, canonical
from personal_agent.runtime.run_store import RunStateError


def freeze_comparisons(metadata, previous, *, task_id, revision):
    result = copy.deepcopy(metadata)
    old = (previous or {}).get('comparisons', [])
    requested = result.get('comparisons', old)
    if previous is not None:
        if canonical(requested) != canonical(old):
            raise RunStateError('comparison_contract_changed')
        result['comparisons'] = copy.deepcopy(old)
        return result
    frozen = []
    for request in requested:
        if 'comparison_ref' in request:
            raise RunStateError('unknown_comparison_ref')
        identity = [task_id, revision, request]
        ref = 'comparison_' + hashlib.sha256(canonical(identity).encode()).hexdigest()
        frozen.append({**request, 'comparison_ref':ref})
    if frozen:
        result['comparisons'] = frozen
    return result


def validate_evidence_scope(evidence, metadata):
    """Match recognized business filters; an uncheckable constraint fails closed.

    Trip tags use the existing Finance name_contains contract, which stores
    the literal #tag in ledger names. This does not resolve or guess trip tags.
    """
    constraints = (metadata or {}).get('constraints', [])
    if not constraints:
        return
    filters = evidence.get('query_result', {}).get('filters_applied')
    if not isinstance(filters, dict):
        raise AnswerError('evidence_scope_unverifiable')
    for constraint in constraints:
        key, value = constraint['key'], constraint['value']
        if key == 'trip_tag':
            valid = isinstance(value, str) and value and ('#' + value) in filters.get('name_contains', [])
        elif key == 'category':
            valid = filters.get('categories') == [value]
        elif key == 'is_family_expense':
            expected = str(value).lower() if type(value) is bool else value
            valid = filters.get(key) == expected
        elif key in {'date_range', 'categories', 'name_contains', 'personal_amount_cny'}:
            valid = key in filters and canonical(filters[key]) == canonical(value)
        else:
            valid = False
        if not valid:
            raise AnswerError('evidence_scope_mismatch')
