"""Cookie rotation (`__Secure-1PSIDTS` renewal) and the client-side failover logic."""
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from gemini_web2api import rotation
from gemini_web2api.protocol import BardError, ProtocolFrameError

COOKIES = ("SID=abc; HSID=def; SSID=ghi; APISID=jkl; SAPISID=mnop; "
           "__Secure-1PSID=psid-value; __Secure-1PSIDTS=old-ts")


class CookieStringTests(unittest.TestCase):
    def test_parse_and_format_roundtrip(self):
        pairs = rotation.parse_cookies("a=1; b=2;c=3; broken; =empty")
        self.assertEqual(pairs, {"a": "1", "b": "2", "c": "3"})
        self.assertEqual(rotation.format_cookies(pairs), "a=1; b=2; c=3")

    def test_parse_tolerates_empty_input(self):
        self.assertEqual(rotation.parse_cookies(None), {})
        self.assertEqual(rotation.parse_cookies(""), {})

    def test_merge_replaces_existing_and_appends_new(self):
        merged = rotation.merge_set_cookie(
            "a=1; __Secure-1PSIDTS=old",
            ["__Secure-1PSIDTS=new; Path=/; Domain=.google.com; Secure; HttpOnly",
             "NEW_COOKIE=xyz; Path=/"])
        pairs = rotation.parse_cookies(merged)
        self.assertEqual(pairs["a"], "1")
        self.assertEqual(pairs["__Secure-1PSIDTS"], "new")
        self.assertEqual(pairs["NEW_COOKIE"], "xyz")

    def test_merge_ignores_valueless_and_empty_headers(self):
        self.assertEqual(rotation.merge_set_cookie("a=1", ["", None, "novalue"]), "a=1")
        self.assertEqual(rotation.merge_set_cookie("a=1", ["a=1"]), "a=1")

    def test_has_rotatable_session_requires_both_cookies(self):
        self.assertTrue(rotation.has_rotatable_session(COOKIES))
        self.assertFalse(rotation.has_rotatable_session("SAPISID=only"))
        self.assertFalse(rotation.has_rotatable_session("__Secure-1PSID=only"))


class FakeResponse:
    def __init__(self, set_cookie=(), status=200):
        self.headers = _Headers(set_cookie)
        self.status = status

    def close(self):
        pass


class _Headers:
    def __init__(self, values):
        self._values = list(values)

    def get_all(self, name):
        return list(self._values) if name.lower() == "set-cookie" else None


class FakeOpener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class RotateCookiesTests(unittest.TestCase):
    def setUp(self):
        rotation.reset_throttle()

    def test_rotate_posts_the_expected_request_and_merges_cookies(self):
        opener = FakeOpener(FakeResponse(["__Secure-1PSIDTS=fresh; Path=/"]))
        updated = rotation.rotate_cookies(COOKIES, opener=opener, log=lambda *_: None)
        self.assertIn("__Secure-1PSIDTS=fresh", updated)
        self.assertIn("__Secure-1PSID=psid-value", updated)

        request, timeout = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, rotation.ROTATE_URL)
        self.assertEqual(request.data, rotation.ROTATE_BODY.encode())
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertIsNotNone(timeout)

    def test_rotate_skips_accounts_without_session_cookies(self):
        with self.assertRaises(rotation.RotationSkipped):
            rotation.rotate_cookies("SAPISID=only", opener=FakeOpener(FakeResponse()))
        with self.assertRaises(rotation.RotationSkipped):
            rotation.rotate_cookies("", opener=FakeOpener(FakeResponse()))

    def test_rotation_is_throttled(self):
        opener = FakeOpener(FakeResponse(["__Secure-1PSIDTS=fresh; Path=/"]))
        rotation.rotate_cookies(COOKIES, opener=opener, log=lambda *_: None)
        with self.assertRaises(rotation.RotationSkipped):
            rotation.rotate_cookies(COOKIES, opener=opener, log=lambda *_: None)
        self.assertEqual(len(opener.requests), 1)

    def test_throttle_can_be_disabled_per_call(self):
        opener = FakeOpener(FakeResponse(["__Secure-1PSIDTS=fresh; Path=/"]))
        rotation.rotate_cookies(COOKIES, opener=opener, min_interval=0, log=lambda *_: None)
        rotation.rotate_cookies(COOKIES, opener=opener, min_interval=0, log=lambda *_: None)
        self.assertEqual(len(opener.requests), 2)

    def test_http_error_propagates(self):
        error = urllib.error.HTTPError(rotation.ROTATE_URL, 401, "unauthorized", {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            rotation.rotate_cookies(COOKIES, opener=FakeOpener(error), log=lambda *_: None)

    def test_rotation_that_drops_the_session_cookie_is_an_error(self):
        opener = FakeOpener(FakeResponse(["__Secure-1PSIDTS=fresh; Path=/"]))
        with mock.patch.object(rotation, "merge_set_cookie", return_value="nothing=useful"):
            with self.assertRaises(RuntimeError):
                rotation.rotate_cookies(COOKIES, opener=opener, log=lambda *_: None)

    def test_throttle_key_differs_per_account(self):
        opener = FakeOpener(FakeResponse(["__Secure-1PSIDTS=fresh; Path=/"]))
        rotation.rotate_cookies(COOKIES, opener=opener, log=lambda *_: None)
        other = COOKIES.replace("psid-value", "another-psid")
        rotation.rotate_cookies(other, opener=opener, log=lambda *_: None)
        self.assertEqual(len(opener.requests), 2)


class PersistTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="gw2a-rotation-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.directory, ignore_errors=True))

    def path(self, name="cookie.txt"):
        return os.path.join(self.directory, name)

    def test_plain_file_is_rewritten_in_place(self):
        path = self.path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(COOKIES + "\n")
        self.assertTrue(rotation.persist_cookies(path, "SAPISID=new; __Secure-1PSID=psid-value"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "SAPISID=new; __Secure-1PSID=psid-value")

    def test_json_file_keeps_its_shape_and_extra_keys(self):
        path = self.path("cookie.json")
        payload = {"cookie": COOKIES, "sapisid": "mnop", "note": "keep me"}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        rotation.persist_cookies(path, "SAPISID=rotated; __Secure-1PSID=psid-value")
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored["note"], "keep me")
        self.assertEqual(stored["sapisid"], "rotated")
        self.assertIn("SAPISID=rotated", stored["cookie"])

    def test_persist_creates_missing_directories(self):
        path = os.path.join(self.directory, "nested", "deep", "cookie.txt")
        self.assertTrue(rotation.persist_cookies(path, COOKIES))
        self.assertTrue(os.path.exists(path))

    def test_persist_without_path_is_a_noop(self):
        self.assertFalse(rotation.persist_cookies("", COOKIES))

    def test_renew_cookie_file_rewrites_only_when_cookies_changed(self):
        path = self.path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(COOKIES + "\n")
        with mock.patch.object(rotation, "rotate_cookies", return_value=COOKIES):
            self.assertFalse(rotation.renew_cookie_file(path, log=lambda *_: None))
        with mock.patch.object(rotation, "rotate_cookies",
                               return_value=COOKIES.replace("old-ts", "new-ts")):
            self.assertTrue(rotation.renew_cookie_file(path, log=lambda *_: None))
        with open(path, encoding="utf-8") as handle:
            self.assertIn("__Secure-1PSIDTS=new-ts", handle.read())

    def test_renew_cookie_file_swallows_renewal_failures(self):
        path = self.path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(COOKIES + "\n")
        with mock.patch.object(rotation, "rotate_cookies",
                               side_effect=urllib.error.URLError("offline")):
            self.assertFalse(rotation.renew_cookie_file(path, log=lambda *_: None))

    def test_renew_cookie_file_handles_missing_file(self):
        self.assertFalse(rotation.renew_cookie_file(self.path("absent.txt"),
                                                    log=lambda *_: None))

    def test_renew_cookie_file_reads_json_shape(self):
        path = self.path("cookie.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"cookie": COOKIES, "sapisid": "mnop"}, handle)
        with mock.patch.object(rotation, "rotate_cookies",
                               return_value=COOKIES.replace("old-ts", "new-ts")) as rotate:
            self.assertTrue(rotation.renew_cookie_file(path, log=lambda *_: None))
        self.assertEqual(rotate.call_args.args[0], COOKIES)


class ClientFailureClassificationTests(unittest.TestCase):
    """The account-failover rules that decide renew / switch / give up."""

    def test_http_status_reads_every_error_shape(self):
        from gemini_web2api.gemini import _http_status

        self.assertEqual(_http_status(urllib.error.HTTPError("u", 403, "x", {}, None)), 403)
        self.assertEqual(_http_status(BardError(1095)), 429)
        self.assertEqual(_http_status(ValueError("bad header")), 0)
        self.assertEqual(_http_status(RuntimeError("boom")), 0)

    def test_account_errors_trigger_failover(self):
        from gemini_web2api.gemini import _is_account_error

        self.assertTrue(_is_account_error(urllib.error.HTTPError("u", 401, "x", {}, None)))
        self.assertTrue(_is_account_error(urllib.error.HTTPError("u", 429, "x", {}, None)))
        self.assertTrue(_is_account_error(BardError(1037)))
        self.assertTrue(_is_account_error(BardError(1060)))
        self.assertTrue(_is_account_error(BardError(1095)))
        self.assertTrue(_is_account_error(ValueError("invalid header")))

    def test_non_account_errors_do_not_trigger_failover(self):
        from gemini_web2api.gemini import _is_account_error

        self.assertFalse(_is_account_error(ProtocolFrameError("damaged frame")))
        self.assertFalse(_is_account_error(BardError(1050)))
        self.assertFalse(_is_account_error(BardError(1052)))
        self.assertFalse(_is_account_error(urllib.error.HTTPError("u", 500, "x", {}, None)))
        self.assertFalse(_is_account_error(RuntimeError("network down")))

    def test_only_credential_errors_justify_a_rotation(self):
        from gemini_web2api.gemini import _is_credential_error

        self.assertTrue(_is_credential_error(urllib.error.HTTPError("u", 403, "x", {}, None)))
        self.assertTrue(_is_credential_error(ValueError("invalid header")))
        self.assertFalse(_is_credential_error(urllib.error.HTTPError("u", 429, "x", {}, None)))
        self.assertFalse(_is_credential_error(BardError(1095)))


if __name__ == "__main__":
    unittest.main()
