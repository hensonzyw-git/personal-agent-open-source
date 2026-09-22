"""Validate bounded native Git objects before any credential-bearing request."""
import base64
import hashlib
import re
from datetime import datetime,timezone

SECRET=re.compile(rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]+|AKIA[A-Z0-9]{16})\b')
SHA=re.compile('[a-f0-9]{40}')


def object_sha(kind,raw):
    return hashlib.sha1(kind.encode()+b' '+str(len(raw)).encode()+b'\0'+raw).hexdigest()


def decode_bundle(bundle,commits):
    if not isinstance(bundle,list) or not 1<=len(bundle)<=512:raise ValueError('GIT_BUNDLE_INVALID')
    objects={};total=0
    for item in bundle:
        if not isinstance(item,dict) or set(item)!={'sha','kind','data'} or item['kind'] not in ('blob','tree','commit'):
            raise ValueError('GIT_BUNDLE_INVALID')
        try:raw=base64.b64decode(item['data'],validate=True)
        except (ValueError,TypeError):raise ValueError('GIT_BUNDLE_INVALID') from None
        # Leak checks precede semantic Git parsing and hashing.
        if SECRET.search(raw):raise ValueError('GIT_BUNDLE_SECRET')
        total+=len(raw)
        if total>1024*1024:raise ValueError('GIT_BUNDLE_LIMIT')
        if object_sha(item['kind'],raw)!=item['sha'] or item['sha'] in objects:raise ValueError('GIT_OBJECT_INVALID')
        objects[item['sha']]=(item['kind'],raw)
    expected={commit['sha']:commit for commit in commits}
    if {sha for sha,(kind,_) in objects.items() if kind=='commit'}!=set(expected):raise ValueError('GIT_COMMIT_SET_INVALID')
    visited=set();ordered=[]
    def visit(sha,expected_kind):
        if sha not in objects:return
        if sha in visited:return
        kind,raw=objects[sha]
        if kind!=expected_kind:raise ValueError('GIT_OBJECT_TYPE_INVALID')
        visited.add(sha)
        if kind=='blob':payload={'content':base64.b64encode(raw).decode(),'encoding':'base64'}
        elif kind=='tree':
            entries=[];offset=0;names=set()
            while offset<len(raw):
                end=raw.find(b'\0',offset)
                if end<0 or end+21>len(raw):raise ValueError('GIT_TREE_INVALID')
                try:mode,name=raw[offset:end].split(b' ',1);name=name.decode('utf-8')
                except (ValueError,UnicodeError):raise ValueError('GIT_TREE_INVALID') from None
                if mode not in (b'100644',b'100755',b'40000') or name in ('','.','..','.git') or '/' in name or name in names:raise ValueError('GIT_TREE_INVALID')
                names.add(name);child=raw[end+1:end+21].hex();childkind='tree' if mode==b'40000' else 'blob'
                visit(child,childkind)
                entries.append(dict(path=name,mode='040000' if childkind=='tree' else mode.decode(),type=childkind,sha=child))
                offset=end+21
            payload={'tree':entries}
        else:
            match=re.fullmatch(rb'tree ([a-f0-9]{40})\nparent ([a-f0-9]{40})\nauthor DAL <dal@localhost> ([0-9]+) \+0000\ncommitter DAL <dal@localhost> ([0-9]+) \+0000\n\n(DAL stage [A-Za-z0-9_.:-]+/[1-9][0-9]*\n)',raw)
            if match is None or match[3]!=match[4]:raise ValueError('GIT_COMMIT_INVALID')
            tree,parent=match[1].decode(),match[2].decode()
            if (tree,parent)!=(expected[sha]['tree'],expected[sha]['parent']):raise ValueError('GIT_COMMIT_BINDING_INVALID')
            visit(parent,'commit');visit(tree,'tree')
            identity=dict(name='DAL',email='dal@localhost',date=datetime.fromtimestamp(int(match[3]),timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
            payload=dict(message=match[5].decode(),tree=tree,parents=[parent],author=identity,committer=identity)
        ordered.append((sha,kind,payload))
    for commit in commits:visit(commit['sha'],'commit')
    if visited!=set(objects):raise ValueError('GIT_UNREACHABLE_OBJECTS')
    return ordered
