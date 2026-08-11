"""Regression tests for the process-wide default HTTP timeout.

Guards the 2026-08-11 hang: the splitwise SDK builds its own requests.Session
and calls session.send() with no timeout, so a stalled connection blocked the
run for the full 15-minute systemd TimeoutStartSec. socket.setdefaulttimeout()
(the 2026-07-16 attempt) does not fix this, because requests turns its default
timeout=None into an explicit sock.settimeout(None).
"""

import socket
import threading
import time
import unittest

import requests

from main import DEFAULT_HTTP_TIMEOUT, install_default_http_timeout


class StalledServer:
    """TCP listener that accepts a connection and then never replies."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._accepted = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        # Hold the connection open without responding.
        self._accepted.append(conn)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/"

    def close(self):
        for conn in self._accepted:
            conn.close()
        self._sock.close()


class TestDefaultHTTPTimeout(unittest.TestCase):
    def setUp(self):
        self._original_send = requests.Session.send
        self._original_default = socket.getdefaulttimeout()
        self.server = StalledServer()
        self.addCleanup(self.server.close)

    def tearDown(self):
        requests.Session.send = self._original_send
        socket.setdefaulttimeout(self._original_default)

    def test_timeoutless_request_gets_default_timeout(self):
        """A caller that sets no timeout must still time out, not hang.

        This is exactly how the splitwise SDK issues its requests.
        """
        install_default_http_timeout(timeout=(2, 2))
        start = time.time()
        with self.assertRaises(requests.exceptions.Timeout):
            requests.Session().send(
                requests.Request("GET", self.server.url).prepare()
            )
        self.assertLess(time.time() - start, 10)

    def test_socket_default_timeout_alone_does_not_cover_requests(self):
        """Pin the reason the 2026-07-16 fix silently did nothing.

        If a future requests/urllib3 version ever makes setdefaulttimeout()
        apply on its own, this test fails and the comments explaining why the
        Session.send patch exists can be revisited.
        """
        socket.setdefaulttimeout(2)
        start = time.time()
        try:
            requests.Session().send(
                requests.Request("GET", self.server.url).prepare(),
                timeout=6,  # bound the test; the point is 2s did not apply
            )
        except requests.exceptions.Timeout:
            pass
        # Timed out at the explicit 6s, not the 2s socket default.
        self.assertGreater(time.time() - start, 4)

    def test_explicit_caller_timeout_is_preserved(self):
        """ynab.py passes its own timeout; the patch must not override it."""
        install_default_http_timeout(timeout=(30, 30))
        start = time.time()
        with self.assertRaises(requests.exceptions.Timeout):
            requests.Session().send(
                requests.Request("GET", self.server.url).prepare(),
                timeout=(2, 2),
            )
        self.assertLess(time.time() - start, 10)

    def test_install_is_idempotent(self):
        """Repeated installs must not stack wrappers."""
        install_default_http_timeout()
        once = requests.Session.send
        install_default_http_timeout()
        self.assertIs(requests.Session.send, once)

    def test_default_timeout_is_bounded(self):
        connect, read = DEFAULT_HTTP_TIMEOUT
        self.assertTrue(0 < connect <= 60)
        self.assertTrue(0 < read <= 300)


if __name__ == "__main__":
    unittest.main()
