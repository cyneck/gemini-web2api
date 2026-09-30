"""Server-side boundary tests.

These exercise the pieces that protect the process itself -- socket timeouts,
body caps, console CSRF, key checking and CORS -- rather than the Gemini
protocol, which lives in test_protocol.py.
"""
import http.client
import json
import socket
import threading
import time
import unittest

from gemini_web2api.config import CONFIG
from gemini_web2api.server import (GeminiHandler, ThreadedServer, _socket_timeout,
                                   _usage)


class SocketTimeoutConfigTests(unittest.TestCase):
    """The mapping from config to the value socketserver actually uses."""

    def setUp(self):
        self.original = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original)

    def test_default_is_five_minutes(self):
        CONFIG.pop("client_socket_timeout_sec", None)
        self.assertEqual(_socket_timeout(), 300)

    def test_zero_disables_the_guard(self):
        # socketserver only calls settimeout() for a non-None value, and None
        # is exactly "block forever", so 0 has to become None rather than 0.
        CONFIG["client_socket_timeout_sec"] = 0
        self.assertIsNone(_socket_timeout())

    def test_negative_is_treated_as_disabled(self):
        CONFIG["client_socket_timeout_sec"] = -5
        self.assertIsNone(_socket_timeout())

    def test_garbage_falls_back_to_the_default(self):
        for value in ("soon", None, [], {}):
            CONFIG["client_socket_timeout_sec"] = value
            self.assertEqual(_socket_timeout(), 300, f"value={value!r}")

    def test_explicit_value_is_used(self):
        CONFIG["client_socket_timeout_sec"] = 42
        self.assertEqual(_socket_timeout(), 42)


class UsageAccountingTests(unittest.TestCase):
    def test_token_estimate_is_roughly_chars_over_four(self):
        self.assertEqual(
            _usage("abcd" * 10, "efgh" * 5),
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    def test_missing_values_do_not_crash(self):
        self.assertEqual(_usage(None, None)["total_tokens"], 0)


class LiveServerTests(unittest.TestCase):
    """A real ThreadedServer on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original = dict(CONFIG)
        CONFIG["log_requests"] = False
        CONFIG["api_keys"] = []

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original)

    # ─── helpers ────────────────────────────────────────────────────────────

    def request(self, method, path, body=None, headers=None, port=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", port or self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            payload = response.read().decode()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    # ─── liveness ───────────────────────────────────────────────────────────

    def test_healthz_reports_version_and_model_count(self):
        status, _, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["version"])
        self.assertGreater(data["models"], 0)

    def test_unknown_path_is_not_found(self):
        status, _, _ = self.request("GET", "/definitely-not-a-route")
        self.assertEqual(status, 404)

    # ─── slow-client guard ──────────────────────────────────────────────────

    def test_half_sent_request_is_dropped(self):
        """A client that stalls mid-header must not hold a thread forever."""
        CONFIG["client_socket_timeout_sec"] = 1
        client = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            client.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n")
            # No terminating blank line: the server is now blocked reading
            # headers. It should give up after the configured second.
            started = time.monotonic()
            client.settimeout(8)
            try:
                chunk = client.recv(64)
            except socket.timeout:
                self.fail("server kept the stalled connection open past the timeout")
            elapsed = time.monotonic() - started
            self.assertEqual(chunk, b"", "server should have closed the socket")
            self.assertLess(elapsed, 6, "guard fired far later than configured")
        finally:
            client.close()

    def test_healthy_request_is_not_dropped_by_the_guard(self):
        CONFIG["client_socket_timeout_sec"] = 1
        status, _, _ = self.request("GET", "/healthz")
        self.assertEqual(status, 200)

    # ─── body cap ───────────────────────────────────────────────────────────

    def test_oversized_body_is_rejected_with_413(self):
        CONFIG["max_request_body_bytes"] = 2048
        payload = json.dumps({"model": "gemini-auto",
                              "messages": [{"role": "user", "content": "a" * 5000}]})
        status, _, body = self.request(
            "POST", "/v1/chat/completions", body=payload,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(payload))})
        self.assertEqual(status, 413)
        self.assertIn("max_request_body_bytes", body)

    def test_body_within_the_cap_is_not_rejected(self):
        CONFIG["max_request_body_bytes"] = 65536
        # An unparseable body proves the cap let it through and the failure
        # came from the JSON layer instead.
        status, _, _ = self.request(
            "POST", "/v1/chat/completions", body=b"not json",
            headers={"Content-Type": "application/json"})
        self.assertNotEqual(status, 413)

    # ─── console CSRF ───────────────────────────────────────────────────────

    def test_form_encoded_console_post_is_rejected(self):
        status, _, body = self.request(
            "POST", "/api/config", body="key=value",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        self.assertIn("application/json", body)

    def test_cross_origin_console_post_is_rejected(self):
        CONFIG["cors_origins"] = []
        status, _, body = self.request(
            "POST", "/api/config", body="{}",
            headers={"Content-Type": "application/json",
                     "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", body)

    def test_cross_origin_is_still_rejected_when_another_origin_is_allowed(self):
        CONFIG["cors_origins"] = ["https://trusted.example"]
        status, _, _ = self.request(
            "POST", "/api/config", body="{}",
            headers={"Content-Type": "application/json",
                     "Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    # ─── CORS echo ──────────────────────────────────────────────────────────

    def test_no_cors_header_when_nothing_is_allow_listed(self):
        CONFIG["cors_origins"] = []
        _, headers, _ = self.request("GET", "/healthz")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_listed_origin_is_echoed_with_vary(self):
        CONFIG["cors_origins"] = ["https://trusted.example"]
        _, headers, _ = self.request(
            "GET", "/healthz", headers={"Origin": "https://trusted.example"})
        self.assertEqual(headers.get("Access-Control-Allow-Origin"),
                         "https://trusted.example")
        self.assertEqual(headers.get("Vary"), "Origin")

    def test_unlisted_origin_is_not_echoed(self):
        CONFIG["cors_origins"] = ["https://trusted.example"]
        _, headers, _ = self.request(
            "GET", "/healthz", headers={"Origin": "https://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    # ─── API key enforcement ────────────────────────────────────────────────

    def test_v1_requests_are_open_when_no_keys_are_configured(self):
        CONFIG["api_keys"] = []
        status, _, _ = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)

    def test_v1_requests_require_a_key_once_one_is_configured(self):
        CONFIG["api_keys"] = ["s3cret"]
        status, _, _ = self.request("GET", "/v1/models")
        self.assertEqual(status, 401)

    def test_correct_key_is_accepted(self):
        CONFIG["api_keys"] = ["s3cret"]
        status, _, _ = self.request(
            "GET", "/v1/models", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 200)

    def test_wrong_key_is_rejected_and_wildcards_do_not_match(self):
        CONFIG["api_keys"] = ["s3cret"]
        for candidate in ("s3cret2", "s3cre", "", "s3cret "):
            status, _, _ = self.request(
                "GET", "/v1/models",
                headers={"Authorization": f"Bearer {candidate}"})
            self.assertEqual(status, 401, f"candidate={candidate!r}")

    def test_any_of_several_keys_is_accepted(self):
        CONFIG["api_keys"] = ["first", "second", "third"]
        for key in ("first", "second", "third"):
            status, _, _ = self.request(
                "GET", "/v1/models", headers={"Authorization": f"Bearer {key}"})
            self.assertEqual(status, 200, f"key={key!r}")

    def test_api_key_supplied_as_x_api_key_header(self):
        CONFIG["api_keys"] = ["s3cret"]
        status, _, _ = self.request("GET", "/v1/models",
                                    headers={"x-api-key": "s3cret"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
