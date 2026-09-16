"""Closed business commands for one explicitly selected report-only role."""
import json
from pathlib import Path
from pydantic import TypeAdapter
from personal_agent_dal.machine.workflow_selection import Id, digest
from personal_agent_dal.machine.execution_start import PrepareExecutionRequest, StartExecutionRequest
from personal_agent_dal.service.execution_routes import RegisterRequest

COMMANDS = ('register', 'prepare', 'start', 'report')


def add_commands(sub):
    register = sub.add_parser('register', help='register a pending task; creates no Job')
    register.add_argument('--input-file', type=Path, required=True, help='closed task/repository/base/toolchain JSON')
    register.add_argument('--output-file', type=Path)
    prepare = sub.add_parser('prepare', help='prepare one report_only role; creates no Job')
    prepare.add_argument('--input-file', type=Path, required=True, help='execution_input JSON returned by register')
    prepare.add_argument('--feature-id', required=True)
    prepare.add_argument('--request-id', required=True)
    prepare.add_argument('--action-key', required=True)
    prepare.add_argument('--role', choices=('planner','coder','reviewer'), required=True)
    prepare.add_argument('--profile-revision-id', required=True)
    prepare.add_argument('--feature-version', type=int, required=True)
    prepare.add_argument('--gate-version', type=int, required=True)
    prepare.add_argument('--output-file', type=Path)
    start = sub.add_parser('start', help='consume an explicitly confirmed execution digest')
    start.add_argument('--prepared-file', type=Path, required=True)
    start.add_argument('--confirmed-digest', required=True)
    start.add_argument('--request-id', required=True)
    start.add_argument('--expires-at', type=int, required=True)
    start.add_argument('--yes', action='store_true')
    report = sub.add_parser('report', help='retrieve stored execution evidence; requires read authorization')
    report.add_argument('attempt_id')


def _read(path):
    if path.stat().st_size > 262144:
        raise ValueError('INPUT_TOO_LARGE')
    return json.loads(path.read_text(encoding='utf-8'))


def _display(prepared):
    c = prepared['confirmation']
    if digest(c) != prepared['confirmed_execution_sha256']:
        raise ValueError('CONFIRMATION_DIGEST_MISMATCH')
    if digest(prepared['profile']) != c['snapshot_sha256'] or digest(prepared['execution_input']) != c['input_binding_sha256']:
        raise ValueError('DISPLAY_BINDING_MISMATCH')
    if c['completion_mode'] != 'report_only':
        raise ValueError('COMPLETION_MODE_INVALID')
    # Full input, actual role configuration, budgets and versions are displayed.
    print(json.dumps(prepared, ensure_ascii=False, indent=2))
    print('report_only: execution report does not complete the Feature')


def run(args, token):
    from personal_agent_dal.service.operator_cli import _request, _fail_with_envelope, _require_dict
    try:
        if args.command == 'register':
            payload = RegisterRequest.model_validate(_read(args.input_file)).model_dump()
            route, method = '/operator/tasks/register', 'POST'
        elif args.command == 'prepare':
            feature = TypeAdapter(Id).validate_python(args.feature_id)
            payload = PrepareExecutionRequest(request_id=args.request_id, action_key=args.action_key,
                execution_role=args.role, execution_input=_read(args.input_file),
                profile_revision_id=args.profile_revision_id, expected_feature_version=args.feature_version,
                expected_gate_version=args.gate_version, completion_mode='report_only').model_dump(by_alias=True)
            route, method = f'/operator/features/{feature}/execution-selections', 'POST'
        elif args.command == 'start':
            prepared = _read(args.prepared_file)
            _display(prepared)
            c = prepared['confirmation']
            if args.confirmed_digest != prepared['confirmed_execution_sha256']:
                raise ValueError('EXPLICIT_CONFIRMATION_MISMATCH')
            feature = TypeAdapter(Id).validate_python(prepared['execution_input']['feature_id'])
            payload = StartExecutionRequest(request_id=args.request_id, action_id=c['action_id'],
                selection_id=c['selection_id'], expected_feature_version=c['feature_version'],
                expected_gate_version=c['gate_version'], expected_action_version=c['action_version'],
                confirmed_execution_sha256=args.confirmed_digest, expires_at=args.expires_at).model_dump()
            if not args.yes and input('Start this displayed execution? [y/N] ').strip().lower() != 'y':
                print('aborted')
                return 1
            route, method = f'/operator/features/{feature}/executions', 'POST'
        else:
            attempt = TypeAdapter(Id).validate_python(args.attempt_id)
            route, method, payload = f'/operator/provider-attempts/{attempt}/report', 'GET', None
    except (ValueError, OSError, KeyError, TypeError):
        raise SystemExit('Invalid execution command input') from None
    status, body = _request(method, args.base_url.rstrip('/')+route, token, payload=payload, timeout=args.timeout)
    if status != 200:
        _fail_with_envelope(status, body)
    body = _require_dict(body)
    if args.command == 'prepare':
        _display(body)
    else:
        print(json.dumps(body, ensure_ascii=False, indent=2))
    output = getattr(args, 'output_file', None)
    if output:
        # Never overwrite an existing operator evidence file.
        with output.open('x', encoding='utf-8') as stream:
            json.dump(body, stream, ensure_ascii=False, indent=2)
    return 0
