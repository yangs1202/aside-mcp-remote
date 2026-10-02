import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import server


class SessionLifecycleTest(unittest.TestCase):
    def tearDown(self):
        for sid in list(server.sessions):
            server.close_session(sid)

    def session(self, age=0):
        session = mock.Mock()
        session.lock = threading.RLock()
        session.last_used = time.monotonic() - age
        session.process.poll.return_value = None
        return session

    def test_expired_and_dead_sessions_are_closed(self):
        expired = self.session(server.MCP_SESSION_TTL + 1)
        dead = self.session()
        dead.process.poll.return_value = 0
        fresh = self.session()
        server.sessions.update(expired=expired, dead=dead, fresh=fresh)
        server.reap_sessions()
        self.assertEqual(list(server.sessions), ['fresh'])
        expired.close.assert_called_once()
        dead.close.assert_called_once()
        fresh.close.assert_not_called()

    def test_busy_session_is_not_reaped(self):
        busy = self.session(server.MCP_SESSION_TTL + 1)
        server.sessions['busy'] = busy
        ready, release = threading.Event(), threading.Event()
        def hold():
            with busy.lock:
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        ready.wait(2)
        try:
            server.reap_sessions()
            self.assertIn('busy', server.sessions)
            busy.close.assert_not_called()
        finally:
            release.set()
            thread.join()

    def test_close_reaps_process_and_closes_pipes(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   text=True)
        with mock.patch.object(server.subprocess, 'Popen', return_value=process):
            session = server.AsideMcpSession()
        session.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertFalse(session.reader.is_alive())

    def test_capacity_returns_503_without_spawning(self):
        from test_server import OpenAIAdapterTest
        import urllib.error
        httpd = server.Server(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        client = OpenAIAdapterTest()
        client.base_url = f'http://127.0.0.1:{httpd.server_port}'
        try:
            with mock.patch.object(server, 'MCP_MAX_SESSIONS', 0), mock.patch.object(server, 'AsideMcpSession') as spawn:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    client.request('/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'})
                self.assertEqual(error.exception.code, 503)
                error.exception.close()
                spawn.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def test_capacity_retires_oldest_idle_session(self):
        old = self.session(server.MCP_SESSION_PRESSURE_IDLE + 20)
        newer = self.session(server.MCP_SESSION_PRESSURE_IDLE + 10)
        server.sessions.update(newer=newer, old=old)
        with mock.patch.object(server, 'MCP_MAX_SESSIONS', 2):
            with server.sessions_lock:
                self.assertTrue(server.make_session_room())
        self.assertEqual(list(server.sessions), ['newer'])
        old.close.assert_called_once()
        newer.close.assert_not_called()

    def test_capacity_preserves_recent_and_busy_sessions(self):
        recent = self.session()
        busy = self.session(server.MCP_SESSION_PRESSURE_IDLE + 20)
        server.sessions.update(recent=recent, busy=busy)
        ready, release = threading.Event(), threading.Event()
        def hold():
            with busy.lock:
                ready.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(ready.wait(2))
        try:
            with mock.patch.object(server, 'MCP_MAX_SESSIONS', 2):
                with server.sessions_lock:
                    self.assertFalse(server.make_session_room())
            busy.close.assert_not_called()
            recent.close.assert_not_called()
        finally:
            release.set()
            thread.join()

    def test_expired_session_http_404_allows_reinitialization(self):
        import json
        import urllib.error
        import urllib.request
        httpd = server.Server(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for method, rpc_method, expected in [('POST', 'tools/list', 404),
                                                ('POST', 'initialize', 404),
                                                ('DELETE', None, 404)]:
                payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': rpc_method}).encode()
                request = urllib.request.Request(
                    f'http://127.0.0.1:{httpd.server_port}/mcp',
                    data=payload if method == 'POST' else None,
                    headers={'Content-Type': 'application/json', 'Mcp-Session-Id': 'expired'},
                    method=method)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(error.exception.code, expected)
                error.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()
