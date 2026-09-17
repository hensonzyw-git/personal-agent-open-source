"""Fixed offline child. No command, source code, environment or provider input."""
import json
import sys
import time

if __name__ == '__main__':
    mode=sys.argv[1] if len(sys.argv)==2 else ''
    if mode=='timeout':time.sleep(5)
    elif mode=='output':
        for _ in range(1024):print('x'*8192,flush=True)
    elif mode=='events':
        for _ in range(65): print(json.dumps({'type':'thread.started'}),flush=True)
        time.sleep(5)
    elif mode in ('unicode_report','escaped_report'):
        text = '中'*131072 if mode=='unicode_report' else '\x00'*100000
        print(json.dumps({'type':'item.completed','item':{'id':'report','type':'agent_message','text':text}}))
        print(json.dumps({'type':'turn.completed'}))
    elif mode=='malformed':print('{invalid}')
    elif mode=='truncated':sys.stdout.write('{"type":"turn.completed"}')
    elif mode=='provider_error':print(json.dumps({'type':'turn.failed'}))
    elif mode in ('success','stderr_secret','descendant'):
        if mode=='descendant':
            import os
            from pathlib import Path
            child=os.fork()
            if child==0:
                os.close(0);os.close(1);os.close(2)
                time.sleep(20);os._exit(0)
            Path(os.environ['TMPDIR'],'ordinary-child.pid').write_text(str(child))
        if mode=='stderr_secret':print('api_key=synthetic-private-value',file=sys.stderr)
        print(json.dumps({'type':'item.started','item':{'id':'tool-1','type':'command_execution'}}))
        print(json.dumps({'type':'item.completed','item':{'id':'tool-1','type':'command_execution','exit_code':0}}))
        print(json.dumps({'type':'item.completed','item':{'id':'report','type':'agent_message','text':'Synthetic fixture report; production_enabled=false'}}))
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':0,'output_tokens':0}}))
    else:sys.exit(2)
