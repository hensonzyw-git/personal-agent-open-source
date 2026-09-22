"""Actual loopback HTTP counterparty; no provider, credentials or remote host."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from personal_agent.api.dal_timeline_client import TimelineTransport


@pytest.mark.parametrize('status', [429, 500, 502, 503, 504, 400, 401, 403, 302])
def test_temporary_http_failure_uses_background_retry_category(status):
    class Counterparty(BaseHTTPRequestHandler):
        current_status = status

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            body = b'{"synthetic":true}'
            self.send_response(self.current_status)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(('127.0.0.1', 0), Counterparty)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    # Isolate HTTP status classification; production URL pinning is unchanged.
    transport = object.__new__(TimelineTransport)
    transport.base_url = f'http://127.0.0.1:{server.server_port}'
    try:
        temporary = status == 429 or 500 <= status <= 599
        with pytest.raises(OSError if temporary else ValueError, match='DAL_UNAVAILABLE'):
            transport._post('/synthetic', {})
        Counterparty.current_status = 200
        assert transport._post('/synthetic', {}) == {'synthetic': True}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
