"""URL safety checks and bounded downloads (SSRF hardening)."""
import unittest
import urllib.error
from unittest import mock

from gemini_web2api.netguard import (ResponseTooLargeError, UnsafeURLError,
                                     fetch_bytes, is_blocked_ip, validate_url)


class BlockedAddressTests(unittest.TestCase):
    def test_private_and_special_ranges_are_blocked(self):
        blocked = [
            "127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.1.1", "169.254.169.254",
            "0.0.0.0", "224.0.0.1", "100.64.0.1", "198.18.0.1",
            "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1",
        ]
        for address in blocked:
            with self.subTest(address=address):
                self.assertTrue(is_blocked_ip(address), address)

    def test_public_addresses_are_allowed(self):
        for address in ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"]:
            with self.subTest(address=address):
                self.assertFalse(is_blocked_ip(address), address)

    def test_garbage_is_treated_as_blocked(self):
        self.assertTrue(is_blocked_ip("not-an-ip"))


class ValidateUrlTests(unittest.TestCase):
    def test_rejects_unsupported_schemes(self):
        for url in ["ftp://example.com/a.png", "file:///etc/passwd", "gopher://x/1",
                    "data:image/png;base64,AAAA", ""]:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url)

    def test_rejects_hostless_url(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("http:///only-a-path")

    def test_rejects_internal_hostnames(self):
        for host in ["localhost", "metadata.google.internal", "metadata",
                     "localhost.localdomain"]:
            with self.subTest(host=host):
                with self.assertRaises(UnsafeURLError):
                    validate_url(f"http://{host}/latest/meta-data/")

    def test_rejects_private_ipv4_literals(self):
        for url in ["http://127.0.0.1/x.png", "http://10.1.2.3/x.png",
                    "http://169.254.169.254/computeMetadata/v1/"]:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url)

    def test_rejects_obfuscated_loopback_literals(self):
        # urllib/browsers accept these alternative IPv4 notations, so a plain
        # ipaddress() check on the literal string would not catch them.
        for url in ["http://2130706433/x.png", "http://0x7f.0.0.1/x.png",
                    "http://0177.0.0.1/x.png"]:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url)

    def test_rejects_ipv6_loopback(self):
        with self.assertRaises(UnsafeURLError):
            validate_url("http://[::1]/x.png")

    def test_allows_public_ip_literal(self):
        parsed = validate_url("https://93.184.216.34/x.png")
        self.assertEqual(parsed.hostname, "93.184.216.34")

    def test_allow_private_escape_hatch(self):
        parsed = validate_url("http://10.0.0.5/x.png", allow_private=True)
        self.assertEqual(parsed.hostname, "10.0.0.5")


class FakeResponse:
    def __init__(self, body=b"", headers=None, status=200):
        self._body = body
        self.headers = headers or {}
        self.status = status
        self.closed = False
        self.read_calls = []

    def read(self, size=-1):
        self.read_calls.append(size)
        if size is None or size < 0:
            return self._body
        return self._body[:size]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0) if self.outcomes else RuntimeError("no more outcomes")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def redirect(url, location, code=302):
    headers = {"Location": location}
    return urllib.error.HTTPError(url, code, "redirect", headers, None)


class FetchBytesTests(unittest.TestCase):
    def fetch(self, opener, **kwargs):
        with mock.patch("gemini_web2api.netguard._build_opener", return_value=opener):
            return fetch_bytes("https://93.184.216.34/a.png", max_bytes=1024, timeout=5,
                               **kwargs)

    def test_returns_body_and_closes_response(self):
        response = FakeResponse(b"png-bytes")
        opener = FakeOpener([response])
        self.assertEqual(self.fetch(opener), b"png-bytes")
        self.assertTrue(response.closed)
        # Only the capped read is issued, never an unbounded readall.
        self.assertEqual(response.read_calls, [1025])

    def test_rejects_declared_content_length_over_the_cap(self):
        response = FakeResponse(b"x", headers={"Content-Length": "5000"})
        opener = FakeOpener([response])
        with self.assertRaises(ResponseTooLargeError):
            self.fetch(opener)
        self.assertTrue(response.closed)

    def test_rejects_body_over_the_cap(self):
        response = FakeResponse(b"x" * 4096)
        opener = FakeOpener([response])
        with self.assertRaises(ResponseTooLargeError):
            self.fetch(opener)

    def test_follows_a_valid_redirect(self):
        opener = FakeOpener([redirect("https://93.184.216.34/a.png", "https://93.184.216.35/b.png"),
                             FakeResponse(b"final")])
        self.assertEqual(self.fetch(opener), b"final")
        self.assertEqual(len(opener.requests), 2)

    def test_redirect_to_private_address_is_rejected(self):
        opener = FakeOpener([redirect("https://93.184.216.34/a.png", "http://127.0.0.1/secret")])
        with self.assertRaises(UnsafeURLError):
            self.fetch(opener)

    def test_redirect_loop_is_bounded(self):
        opener = FakeOpener([redirect("https://93.184.216.34/a.png", "https://93.184.216.34/a.png")
                             for _ in range(10)])
        with self.assertRaises(UnsafeURLError):
            self.fetch(opener)

    def test_http_error_is_propagated(self):
        error = urllib.error.HTTPError("https://93.184.216.34/a.png", 404, "nope", {}, None)
        opener = FakeOpener([error])
        with self.assertRaises(urllib.error.HTTPError):
            self.fetch(opener)

    def test_blocked_target_never_reaches_the_network(self):
        opener = FakeOpener([])
        with mock.patch("gemini_web2api.netguard._build_opener", return_value=opener):
            with self.assertRaises(UnsafeURLError):
                fetch_bytes("http://169.254.169.254/latest/meta-data/", max_bytes=1024)
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
