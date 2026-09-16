"""Fail-closed public URL and outbound-data checks, independent of ADK."""
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, unquote


class SearchError(ValueError):
    pass


def scan_public_text(text):
    if not isinstance(text,str) or not text.strip() or len(text)>2048:
        raise SearchError('invalid_public_input')
    if re.search(r'(?i)(bearer\s|-----BEGIN|api[_-]?key|password|access[_-]?token|secret|身份证|银行卡|账本|健康记录)',text):
        raise SearchError('outbound_permission_required')


def public_url(url, *, resolve=None):
    scan_public_text(url)
    p=urlsplit(url)
    try:
        if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.fragment or p.port not in {None,80 if p.scheme=='http' else 443}:
            raise ValueError
        host=p.hostname.encode('idna').decode().lower().rstrip('.')
        if '.' not in host or host.endswith(('.local','.internal','.localhost')) or '\\' in url or any(ord(c)<33 for c in url):
            raise ValueError
        try: ipaddress.ip_address(host)
        except ValueError: pass
        else: raise SearchError('nonpublic_url')
        for key,value in parse_qsl(p.query):
            if re.search(r'(?i)(token|key|sig|auth|credential|password)',key): raise ValueError
        if re.search(r'(?i)/(admin|private|internal|account|login|metadata)(/|$)',unquote(p.path)): raise ValueError
        addresses=(resolve(host) if resolve else {r[4][0] for r in socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)})
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses): raise ValueError
    except (ValueError,UnicodeError,OSError):
        raise SearchError('nonpublic_url') from None
    return urlunsplit((p.scheme,host,p.path or '/',p.query,''))
