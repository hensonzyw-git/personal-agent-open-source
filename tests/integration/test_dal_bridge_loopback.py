"""Real local HTTP, synthetic bodies only; not production PA/DAL acceptance."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import httpx
import pytest

from personal_agent.api.dal_client import FixedDalTransport


@pytest.mark.parametrize('status', [200, 302])
def test_fixed_loopback_ignores_proxy_and_does_not_redirect(monkeypatch, status):
    received = []
    expected = {'decision_id': 'synthetic', 'approval_id': 'synthetic', 'status': 'accepted'}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            payload = json.dumps(expected).encode()
            self.send_response(status)
            if status == 302:
                self.send_header('Location', 'http://127.0.0.1:8820/must-not-follow')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    class Server(HTTPServer):
        allow_reuse_address = True

    # Bind before making any request: never contact an existing local service.
    try:
        server = Server(('127.0.0.1', 8820), Handler)
    except OSError as error:
        pytest.skip(f'synthetic loopback listener unavailable: errno={error.errno}')
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
    thread.start()
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    monkeypatch.setenv('no_proxy', '')
    try:
        transport = FixedDalTransport(base_url='http://127.0.0.1:8820', key=None,
                                      kid='synthetic', issuer='synthetic', audience='synthetic')
        if status == 302:
            with pytest.raises(httpx.HTTPStatusError):
                transport.deliver('synthetic-not-a-credential')
        else:
            assert transport.deliver('synthetic-not-a-credential') == expected
        assert received == [('/internal/human-decisions', {'assertion': 'synthetic-not-a-credential'})]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()
