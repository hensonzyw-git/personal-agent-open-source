"""Pinned role launch plans and bounded CLI event decoding. No provider calls."""
from dataclasses import dataclass, field
import json
from pathlib import Path
from personal_agent_dal.worker.supervisor import SupervisorRefusal, verify_executable
from personal_agent_dal.worker.runtime_mapping import resolve_snapshot

@dataclass(frozen=True)
class LaunchPlan:
    argv: tuple[str, ...]
    cwd: str
    environment: dict[str,str]
    runtime: str
    executable_sha256: str
    version: str
    wall_seconds: int = 600
    output_bytes: int = 4194304
    production_enabled: bool = False
    max_steps: int = 64
    fixture_sha256: str | None = None
    auth_route: str = "none"
    auth_home: str | None = None
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    task_directories: dict[str, str] = field(default_factory=dict)
    admission: dict | None = None
    admission_sha256: str | None = None


def build_plan(context, reservation, pins, adapter_config=None):
    pin=resolve_snapshot(context['snapshot'],context['snapshot_sha256'],pins)[context['execution_role']]
    executable=verify_executable({'executable':pin.executable,'executable_sha256':pin.executable_sha256,'version':pin.version})
    role=pin.configuration
    route = validate_auth_route(role, adapter_config, context['execution_role']) if adapter_config is not None else {'mode':'none','home':None,'environment':{}}
    scratch=Path(reservation['temp'])
    # Read-only roles execute tools in a scratch workspace; source is a read root.
    cwd=reservation['workspace'] if role.permission=='workspace_write' else str(scratch)
    env={'PATH':'/usr/bin:/bin','HOME':str(scratch),'TMPDIR':str(scratch),
         'PYTHONDONTWRITEBYTECODE':'1','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',
         'GIT_TERMINAL_PROMPT':'0','GIT_CONFIG_COUNT':'3',
         'GIT_CONFIG_KEY_0':'core.hooksPath','GIT_CONFIG_VALUE_0':'/dev/null',
         'GIT_CONFIG_KEY_1':'core.fsmonitor','GIT_CONFIG_VALUE_1':'false',
         'GIT_CONFIG_KEY_2':'protocol.allow','GIT_CONFIG_VALUE_2':'never'}
    env.update(route['environment'])
    if route['mode']=='codex_login':env['CODEX_HOME']=route['home']
    elif route['mode']=='claude_login':env['CLAUDE_CONFIG_DIR']=route['home']
    writes = (cwd, str(scratch), str(Path(reservation['git'])/'repository')) if role.permission == 'workspace_write' else (str(scratch),)
    reads = (reservation['workspace'], *reservation.get('read_roots', []))
    protected = [*reservation.get('read_roots', []),
        str(Path(reservation['git'])/'repository'/'config'),
        str(Path(reservation['git'])/'repository'/'hooks'),
        *([reservation['workspace']] if role.permission=='read_only' else [])]
    if role.runtime=='codex_cli':
        argv=(str(executable),'exec','--ignore-user-config','--ephemeral','--json','--color','never',
            '--sandbox','workspace-write','--skip-git-repo-check','-C',cwd,'--model',role.model,
            '-c','model_reasoning_effort='+json.dumps(role.reasoning),
            '-c','sandbox_workspace_write.writable_roots='+json.dumps(list(writes)),
            '-c','sandbox_workspace_write.exclude_tmpdir_env_var=true',
            '-c','sandbox_workspace_write.exclude_slash_tmp=true',
            '-c','features.multi_agent=false','-c','approval_policy="never"','-')
    elif role.runtime=='claude_code':
        argv=(str(executable),'--print','--safe-mode','--no-session-persistence','--no-chrome',
            '--output-format','stream-json','--verbose','--model',role.model,'--effort',role.reasoning,
            '--permission-mode','dontAsk','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
            '--max-turns','64','--allowedTools','Read,Glob,Grep,Bash,Edit,Write,WebFetch,WebSearch',
            '--disallowedTools','Agent,Task','--settings',json.dumps({
            'sandbox':{'enabled':True,'autoAllowBashIfSandboxed':True,'allowUnsandboxedCommands':False,
                'filesystem':{'allowWrite':list(writes),'denyWrite':protected}},
            'permissions':{'additionalDirectories':list(reads)+list(writes),
                'deny':[rule for path in protected for rule in ('Edit('+path+'/**)','Write('+path+'/**)','Edit('+path+')','Write('+path+')')]}}))
    else: raise SupervisorRefusal('ROLE_RUNTIME_UNSUPPORTED')
    return LaunchPlan(argv,cwd,env,role.runtime,pin.executable_sha256,pin.version,
        auth_route=route['mode'],auth_home=route['home'],read_roots=reads,write_roots=writes,
        task_directories={name:str(scratch/name) for name in ('scratch','reports','test-copy','cache')})


def parse_events(raw, runtime, *, max_steps=64):
    """Tool use is valid; malformed, incomplete and ambiguous finals fail closed."""
    if len(raw)>4194304 or not raw.endswith(b'\n'):raise SupervisorRefusal('CLI_STREAM_TRUNCATED')
    report=[]; tools=[]; pending=set(); completed=False
    usage={'input_tokens':None,'output_tokens':None,'provider_requests':None}
    outcome='succeeded';reason=None
    try:
        lines = raw.decode('utf-8',errors='strict').splitlines()
        if len(lines) > max_steps: raise SupervisorRefusal('CLI_EVENT_STEP_LIMIT')
        for line in raw.decode('utf-8',errors='strict').splitlines():
            event=json.loads(line)
            if not isinstance(event,dict) or not isinstance(event.get('type'),str) or completed:
                raise ValueError('event shape/order')
            kind=event['type']
            if not kind: raise ValueError('empty event')
            if runtime=='codex_cli':
                if kind in ('item.started','item.updated','item.completed'):
                    item=event['item']; typ=item['type']; ident=item['id']
                    if typ in ('command_execution','mcp_tool_call','web_search','file_change'):
                        if kind!='item.completed':pending.add(ident)
                        else:
                            pending.discard(ident);tools.append(typ)
                    elif typ=='agent_message' and kind=='item.completed':report.append(item['text'])
                    elif typ not in ('reasoning','todo_list','agent_message'):raise ValueError('unknown item')
                elif kind=='turn.completed':
                    completed=True
                    for key in ('input_tokens','output_tokens'):usage[key]=event.get('usage',{}).get(key)
                elif kind in ('turn.failed','error'):
                    outcome='failed';reason='CLI_PROVIDER_ERROR';completed=True
                elif kind not in ('thread.started','turn.started'):raise ValueError('unknown event')
            elif runtime=='claude_code':
                if kind in ('assistant','user'):
                    for block in event['message']['content']:
                        if block['type']=='tool_use':
                            if not isinstance(block['input'],dict) or block['id'] in pending:raise ValueError('tool arguments')
                            pending.add(block['id']);tools.append(block['name'])
                        elif block['type']=='tool_result':pending.discard(block['tool_use_id'])
                        elif block['type']=='text' and kind=='assistant':report.append(block['text'])
                        elif block['type'] not in ('thinking','text'):raise ValueError('unknown content')
                elif kind=='result':
                    completed=True
                    outcome='failed' if event.get('is_error') else 'succeeded'
                    reason='CLI_PROVIDER_ERROR' if outcome=='failed' else None
                    if event.get('permission_denials'):outcome='interaction_required';reason='CLI_INTERACTION_REQUIRED'
                    if event.get('result'):report=[event['result']]
                    for key in ('input_tokens','output_tokens'):usage[key]=event.get('usage',{}).get(key)
                elif kind!='system':raise ValueError('unknown event')
            else:raise ValueError('runtime')
        if not completed or pending or (outcome=='succeeded' and not report):raise ValueError('incomplete')
        if not all(isinstance(x,str) and x for x in tools) or not all(isinstance(x,str) for x in report):raise ValueError('result shape')
        if len(tools)>128 or len('\n'.join(report).encode())>131072:raise SupervisorRefusal('CLI_REPORT_LIMIT')
        for value in usage.values():
            if value is not None and (type(value)!=int or value<0):raise ValueError('usage')
    except SupervisorRefusal:
        raise
    except (ValueError,KeyError,TypeError,UnicodeError,AttributeError) as exc:
        raise SupervisorRefusal('CLI_EVENTS_INVALID') from exc
    return dict(report='\n'.join(report),tool_events=tools,usage=usage,outcome=outcome,reason=reason)


def load_adapter_config(path):
    """Public metadata only. Login stores are references, never opened here."""
    body = json.loads(Path(path).read_text())
    if not isinstance(body, dict) or set(body) != {'schema','roles'} or body['schema'] != 'dal.role-adapters/1.0':
        raise SupervisorRefusal('ADAPTER_CONFIG_INVALID')
    if not isinstance(body['roles'], dict) or set(body['roles']) != {'planner','coder','reviewer'}:
        raise SupervisorRefusal('ADAPTER_ROLE_SET_INVALID')
    for route in body['roles'].values():
        if not isinstance(route, dict) or set(route) != {'mode','home','environment'}:
            raise SupervisorRefusal('AUTH_ROUTE_INVALID')
        if not isinstance(route['environment'], dict): raise SupervisorRefusal('AUTH_ENV_INVALID')
    return body


def validate_auth_route(role, config, name):
    route = config['roles'][name]
    # Existing CLI login modes only. No credential broker or provider discovery.
    if role.runtime == 'codex_cli' and role.provider == 'openai' and role.billing == 'subscription':
        mode = 'codex_login'
    elif role.runtime == 'claude_code' and role.provider == 'anthropic' and role.billing == 'subscription':
        mode = 'claude_login'
    else:
        raise SupervisorRefusal('AUTH_ROUTE_UNSUPPORTED')
    if route['mode'] != mode: raise SupervisorRefusal('AUTH_ROUTE_MISMATCH')
    home = route['home']
    if not isinstance(home,str) or not Path(home).is_absolute() or '..' in Path(home).parts:
        raise SupervisorRefusal('AUTH_HOME_INVALID')
    # No endpoints, proxy variables, API keys, hooks or helper commands may
    # change the subscription route. Secret-bearing paths are only plan refs.
    if route['environment'] != {}: raise SupervisorRefusal('AUTH_ENV_UNSUPPORTED')
    return route
