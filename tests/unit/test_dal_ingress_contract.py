"""Static ingress structure and synthetic transport/shell checks, not live Nginx."""
from pathlib import Path
import subprocess

import httpx
import pytest
from personal_agent.api.dal_client import FixedDalTransport

ROOT = Path(__file__).resolve().parents[2]


def transport(url):
    return FixedDalTransport(base_url=url, key=None, kid='test', issuer='pa', audience='dal')


@pytest.mark.parametrize('url', ['http://127.0.0.1:8820', 'https://dal.invalid', 'https://dal.invalid/'])
def test_exact_destinations(url):
    assert transport(url).base_url == url.rstrip('/')


@pytest.mark.parametrize('url', ['http://127.0.0.1:8820/', ' http://127.0.0.1:8820',
    '\nhttp://127.0.0.1:8820', '\x00http://127.0.0.1:8820', 'http://127.0.0.1:8820\t',
    'http://127.0.0.1:88\n20', 'HTTP://127.0.0.1:8820', 'http://localhost:8820',
    'http://127.0.0.2:8820', 'http://127.0.0.1:80', 'http://127.0.0.1:08820',
    'http://127.1:8820', 'http://[::1]:8820', 'http://2130706433:8820',
    'http://user@127.0.0.1:8820', 'http://127.0.0.1:8820/internal',
    'http://127.0.0.1:8820?', 'http://127.0.0.1:8820#',
    'http://127.0.0.1:8820?x=y', 'http://127.0.0.1:8820#x',
    '\nhttps://dal.invalid', 'https://dal.in\tvalid', ' https://dal.invalid'])
def test_unsafe_url_spellings(url):
    with pytest.raises(ValueError): transport(url)


def test_transport_disables_proxy_and_redirects(monkeypatch):
    original = httpx.Client
    seen = []
    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'Location':'https://elsewhere.invalid'})
    def client(**kwargs):
        assert kwargs == dict(trust_env=False, follow_redirects=False, timeout=10)
        return original(**kwargs, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, 'Client', client)
    with pytest.raises(httpx.HTTPStatusError): transport('http://127.0.0.1:8820').deliver('synthetic')
    assert seen == ['http://127.0.0.1:8820/internal/human-decisions']


def test_nginx_denies_before_rewrite():
    upstream = (ROOT/'deploy/nginx/dal-upstream.conf').read_text()
    guard = 'if ($uri ~ ^(/dal/transport/v1)?/internal(/|$)) { return 403; }'
    assert guard in upstream
    assert upstream.index(guard) < upstream.index('rewrite ^')
    ssl = (ROOT/'deploy/nginx/agent.example.invalid-ssl.conf').read_text()
    for modifier, path in [('=', '/internal'), ('^~', '/internal/'),
                           ('=', '/dal/transport/v1/internal'), ('^~', '/dal/transport/v1/internal/')]:
        assert f'location {modifier} {path} {{ return 403; }}' in ssl
    assert 'location /dal/' not in ssl  # This repair does not activate public routes.


@pytest.mark.parametrize('status,exit_code,accepted', [('403',0,True), ('401',0,False),
    ('200',0,False), ('404',0,False), ('000',7,False), ('403',7,False)])
def test_shell_deny_requires_curl_success_and_exact_403(status, exit_code, accepted):
    script = (ROOT/'deploy/dal-verify.sh').read_text()
    # Source only the pure helper; never execute service/credential/network checks.
    helper = script.split('# BEGIN internal ingress probe\n')[1].split('# END internal ingress probe')[0]
    fake = f'''curl() {{
      [[ " $* " == *" --path-as-is "* ]] || return 99
      [[ " $* " == *" --request POST "* ]] || return 99
      printf '%s' '{status}'
      return {exit_code}
    }}
'''
    result = subprocess.run(['bash', '-c', fake + helper + '\ninternal_ingress_blocked https://synthetic.invalid/internal'],
        capture_output=True, text=True)
    assert (result.returncode == 0) == accepted
    assert result.stdout == ''


def test_probe_has_normalized_variants_and_positive_control():
    script = (ROOT/'deploy/dal-verify.sh').read_text()
    for path in ['/internal', '/dal/transport/v1/internal', '/%69nternal/',
                 '/dal/transport/v1/%69nternal/', '/dal/transport/v1/./internal/',
                 '/dal/transport/v1/x/../internal/', '//internal/', '/dal/transport/v1//internal/']:
        assert '"'+path+'"' in script
    assert '"$OP_CODE" = 401' in script
    assert 'dal.operator-transport/1.0' in script


@pytest.mark.parametrize('body', [b'x'*65537, b'not-json'])
def test_response_remains_bounded_and_json(monkeypatch, body):
    original = httpx.Client
    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    monkeypatch.setattr(httpx, 'Client', client)
    with pytest.raises(ValueError): transport('http://127.0.0.1:8820').deliver('synthetic')
