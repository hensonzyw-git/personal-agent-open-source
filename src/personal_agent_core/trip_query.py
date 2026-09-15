"""Shared, deterministic validation of the trip-query wire extension."""
import re
from decimal import Decimal

TAG_SCHEMA = {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 256, 'pattern': r'^[^#\x00-\x1f\x7f]+$'}
COVERAGE_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['scan_complete', 'scope_coverage', 'assignment_complete', 'source_years', 'unassigned_record_count'],
    'properties': {'scan_complete': {'const': True}, 'scope_coverage': {'enum': ['complete', 'limited', 'unknown']},
        'assignment_complete': {'type': 'boolean'}, 'source_years': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'type': 'integer', 'minimum': 2000, 'maximum': 2100}},
        'unassigned_record_count': {'type': 'integer', 'minimum': 0}}}
BUCKET_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['trip_tag', 'personal_spend_total_cny', 'record_count'],
    'properties': {'trip_tag': TAG_SCHEMA, 'personal_spend_total_cny': {'type': 'string', 'pattern': r'^-?(0|[1-9][0-9]*)\.[0-9]{2}$'},
                   'record_count': {'type': 'integer', 'minimum': 1}}}


def validate_trip_result(data):
    from jsonschema import Draft202012Validator
    fields = {'status', 'view', 'metric', 'record_count', 'filters_applied', 'source_system', 'evidence', 'coverage'}
    fields |= {'records', 'next_cursor'} if data.get('view') == 'records' else {'personal_spend_total_cny'}
    if data.get('view') == 'by_trip':
        fields.add('by_trip')
    if set(data) - fields:
        raise ValueError('unexpected trip result fields')
    coverage = data.get('coverage')
    if not Draft202012Validator(COVERAGE_SCHEMA).is_valid(coverage):
        raise ValueError('invalid trip coverage')
    if coverage['assignment_complete'] != (coverage['unassigned_record_count'] == 0):
        raise ValueError('inconsistent assignment coverage')
    if data['evidence'].get('parser_version') != 'trip-query-v1' or not re.fullmatch('[0-9a-f]{64}', data['evidence'].get('result_checksum', '')):
        raise ValueError('missing trip evidence')
    if data['evidence']['matched_count'] != data['record_count']:
        raise ValueError('inconsistent matched count')
    filters = data['filters_applied']
    if filters.get('categories') != ['旅行']:
        raise ValueError('invalid trip category')
    tag = filters.get('trip_tag')
    if tag is not None and (not isinstance(tag, str) or not tag.strip() or tag != tag.strip() or '#' in tag or len(tag) > 256):
        raise ValueError('invalid exact trip')
    dates = filters.get('date_range')
    if dates is None and coverage['scope_coverage'] != 'unknown':
        raise ValueError('unbounded trip coverage must be unknown')
    if dates is not None:
        from datetime import date
        start, end = date.fromisoformat(dates['start']), date.fromisoformat(dates['end'])
        years = set(coverage['source_years'])
        expected = 'complete' if all(y in years for y in range(start.year, end.year + 1)) else 'limited'
        if start > end or coverage['scope_coverage'] != expected:
            raise ValueError('invalid date coverage')
    if data['view'] == 'records':
        return
    amount = data.get('personal_spend_total_cny')
    if not isinstance(amount, str) or not re.fullmatch(r'-?(0|[1-9][0-9]*)\.[0-9]{2}', amount):
        raise ValueError('invalid trip total')
    if data['view'] != 'by_trip':
        return
    buckets = data.get('by_trip')
    if not isinstance(buckets, list) or len(buckets) > 1000:
        raise ValueError('invalid trip buckets')
    validator = Draft202012Validator(BUCKET_SCHEMA)
    if any(not validator.is_valid(b) for b in buckets):
        raise ValueError('invalid trip bucket')
    import unicodedata
    tags = [b['trip_tag'] for b in buckets]
    if any(t is not None and (t != t.strip() or any(unicodedata.category(c).startswith('C') for c in t)) for t in tags):
        raise ValueError('invalid trip tag characters')
    if len(tags) != len(set(tags)) or (tag is not None and any(t != tag for t in tags)):
        raise ValueError('duplicate or mismatched trip bucket')
    if sum(b['record_count'] for b in buckets) != data['record_count'] or sum((Decimal(b['personal_spend_total_cny']) for b in buckets), Decimal(0)) != Decimal(amount):
        raise ValueError('trip totals do not reconcile')
    if tag is None and sum(b['record_count'] for b in buckets if b['trip_tag'] is None) != coverage['unassigned_record_count']:
        raise ValueError('unassigned count does not reconcile')
    if buckets != sorted(buckets, key=lambda b: (b['trip_tag'] is None, -Decimal(b['personal_spend_total_cny']), b['trip_tag'] or '')):
        raise ValueError('trip order mismatch')


def uses_trip_query(arguments):
    if arguments.get('view') == 'by_trip' or arguments.get('trip_tag') is not None:
        return True
    cursor = arguments.get('cursor')
    if isinstance(cursor, str):
        # Admission can only deny on this hint. Source still verifies its signature.
        import base64, json
        try:
            raw = cursor.split('.')[0]
            return json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4))).get('v') == 3
        except (ValueError, TypeError, AttributeError, UnicodeError):
            pass
    return False
