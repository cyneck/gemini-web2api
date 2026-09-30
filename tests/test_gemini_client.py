"""Client-side failover, delta tracking and text hygiene."""
import unittest
import urllib.error
from unittest import mock

from gemini_web2api import gemini
from gemini_web2api.config import CONFIG
from gemini_web2api.protocol import BardError, Candidate, ProtocolFrameError


def http_error(code):
    return urllib.error.HTTPError("https://gemini.google.com/x", code, "err", {}, None)


class ClientTestCase(unittest.TestCase):
    """Isolate the shared CONFIG dict around every test."""

    def setUp(self):
        self.original = dict(CONFIG)
        CONFIG.clear()
        CONFIG.update(self.original)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original)

    def use_accounts(self, count=2):
        CONFIG["accounts"] = [
            {"auth_user": index, "label": "", "cookie_file": None, "xsrf_token": None,
             "enabled": True}
            for index in range(count)
        ]
        CONFIG["active_account"] = 0


class FailoverTests(ClientTestCase):
    def test_falls_over_to_the_next_account_on_credential_error(self):
        self.use_accounts(2)
        CONFIG["cookie_rotation"] = False
        seen = []

        def fake_once(idx, *args, **kwargs):
            seen.append(idx)
            if idx == 0:
                raise http_error(403)
            return gemini.GenerationResult(text="from account 1")

        with mock.patch.object(gemini, "_generate_once", side_effect=fake_once):
            result = gemini.generate_detailed("prompt", 1, 4)

        self.assertEqual(result.text, "from account 1")
        self.assertEqual(seen, [0, 1])

    def test_rotation_retries_the_same_account_before_switching(self):
        self.use_accounts(2)
        CONFIG["cookie_rotation"] = True
        seen = []
        renewals = []

        def fake_once(idx, *args, **kwargs):
            seen.append(idx)
            if idx == 0:
                raise http_error(403)
            return gemini.GenerationResult(text="second account")

        def fake_renew(idx, error):
            renewals.append(idx)
            # Succeed once, then report that no new cookies were written.
            return len(renewals) == 1

        with mock.patch.object(gemini, "_generate_once", side_effect=fake_once), \
                mock.patch.object(gemini, "_renew_account_cookies", side_effect=fake_renew):
            result = gemini.generate_detailed("prompt", 1, 4)

        self.assertEqual(result.text, "second account")
        self.assertEqual(seen, [0, 0, 1])
        # Only one renewal attempt per account per request.
        self.assertEqual(renewals, [0])

    def test_rotation_result_makes_the_retry_succeed(self):
        self.use_accounts(1)
        CONFIG["cookie_rotation"] = True
        calls = []

        def fake_once(idx, *args, **kwargs):
            calls.append(idx)
            if len(calls) == 1:
                raise http_error(401)
            return gemini.GenerationResult(text="rotated and fine")

        with mock.patch.object(gemini, "_generate_once", side_effect=fake_once), \
                mock.patch.object(gemini, "_renew_account_cookies", return_value=True):
            result = gemini.generate_detailed("prompt", 1, 4)

        self.assertEqual(result.text, "rotated and fine")
        self.assertEqual(calls, [0, 0])

    def test_non_account_error_is_raised_without_failover(self):
        self.use_accounts(2)
        CONFIG["cookie_rotation"] = False
        seen = []

        def fake_once(idx, *args, **kwargs):
            seen.append(idx)
            raise ProtocolFrameError("damaged frame")

        with mock.patch.object(gemini, "_generate_once", side_effect=fake_once):
            with self.assertRaises(ProtocolFrameError):
                gemini.generate_detailed("prompt", 1, 4)

        self.assertEqual(seen, [0])

    def test_single_account_does_not_swallow_the_error(self):
        self.use_accounts(1)
        CONFIG["cookie_rotation"] = False
        with mock.patch.object(gemini, "_generate_once", side_effect=http_error(403)):
            with self.assertRaises(urllib.error.HTTPError):
                gemini.generate_detailed("prompt", 1, 4)

    def test_all_accounts_failing_raises_the_last_error(self):
        self.use_accounts(2)
        CONFIG["cookie_rotation"] = False
        with mock.patch.object(gemini, "_generate_once", side_effect=http_error(429)):
            with self.assertRaises(urllib.error.HTTPError) as context:
                gemini.generate_detailed("prompt", 1, 4)
        self.assertEqual(context.exception.code, 429)

    def test_cookie_override_bypasses_the_account_pool(self):
        self.use_accounts(2)
        with mock.patch.object(gemini, "_generate_once",
                               return_value=gemini.GenerationResult(text="validated")) as once:
            text = gemini.generate("prompt", 1, 4, cookie_str="a=1", sapisid="a")

        self.assertEqual(text, "validated")
        args = once.call_args.args
        self.assertIsNone(args[0])
        self.assertEqual(args[6], "a=1")
        self.assertEqual(args[7], "a")

    def test_capture_receives_the_full_result(self):
        self.use_accounts(1)
        result = gemini.GenerationResult(text="hello", thoughts="thinking",
                                        generated_images=[{"url": "https://x/1.png"}])
        captured = []
        with mock.patch.object(gemini, "_generate_once", return_value=result):
            text = gemini.generate("prompt", 1, 4, capture=captured)

        self.assertEqual(text, "hello")
        self.assertEqual(captured, [result])
        self.assertEqual(captured[0].images[0]["url"], "https://x/1.png")

    def test_bard_error_quota_switches_account_but_refusal_does_not(self):
        self.use_accounts(2)
        CONFIG["cookie_rotation"] = False
        seen = []

        def quota_then_ok(idx, *args, **kwargs):
            seen.append(idx)
            if idx == 0:
                raise BardError(1037)
            return gemini.GenerationResult(text="ok")

        with mock.patch.object(gemini, "_generate_once", side_effect=quota_then_ok):
            self.assertEqual(gemini.generate_detailed("p", 1, 4).text, "ok")
        self.assertEqual(seen, [0, 1])

        seen.clear()

        def refusal(idx, *args, **kwargs):
            seen.append(idx)
            raise BardError(1052)

        with mock.patch.object(gemini, "_generate_once", side_effect=refusal):
            with self.assertRaises(BardError):
                gemini.generate_detailed("p", 1, 4)
        self.assertEqual(seen, [0])


class StreamStateTests(unittest.TestCase):
    def candidates(self, text="", thoughts="", rcid="rc_1"):
        return [Candidate(rcid=rcid, text=text, thoughts=thoughts)]

    def test_emits_only_the_growing_suffix(self):
        state = gemini.StreamState()
        self.assertEqual(list(state.deltas(self.candidates("Hel"))), [("text", "Hel")])
        self.assertEqual(list(state.deltas(self.candidates("Hello"))), [("text", "lo")])
        self.assertEqual(list(state.deltas(self.candidates("Hello"))), [])

    def test_ignores_a_shrinking_value(self):
        state = gemini.StreamState()
        list(state.deltas(self.candidates("Hello")))
        self.assertEqual(list(state.deltas(self.candidates("Hel"))), [])

    def test_rewritten_content_is_reported(self):
        state = gemini.StreamState()
        list(state.deltas(self.candidates("Hello")))
        with self.assertRaises(RuntimeError):
            list(state.deltas(self.candidates("Something else entirely")))

    def test_thoughts_are_tracked_separately(self):
        state = gemini.StreamState()
        events = list(state.deltas(self.candidates("", "think")))
        self.assertEqual(events, [("thinking", "think")])
        events = list(state.deltas(self.candidates("answer", "thinking more")))
        self.assertEqual(events, [("text", "answer"), ("thinking", "ing more")])

    def test_secondary_candidates_are_ignored(self):
        state = gemini.StreamState()
        first = list(state.deltas(self.candidates("primary", rcid="rc_1")))
        second = list(state.deltas(self.candidates("other", rcid="rc_2")))
        self.assertEqual(first, [("text", "primary")])
        self.assertEqual(second, [])

    def test_code_artifacts_are_cleaned_from_deltas(self):
        state = gemini.StreamState()
        raw = "answer\n```python?code_reference&code_event_index=1\nprint(1)\n```\nmore"
        deltas = [delta for _, delta in state.deltas(self.candidates(raw))]
        self.assertIn("answer", deltas[0])
        self.assertNotIn("code_event_index", "".join(deltas))


class TextHygieneTests(unittest.TestCase):
    def test_clean_text_removes_artifacts(self):
        raw = ("hello\n```python?code_reference&code_event_index=2\nprint(1)\n```\n"
               "http://googleusercontent.com/card_content/9\nworld")
        cleaned = gemini.clean_text(raw)
        self.assertEqual(cleaned, "hello\nworld")

    def test_clean_text_can_keep_whitespace(self):
        self.assertEqual(gemini.clean_text("  x  ", strip=False), "  x  ")

    def test_extract_texts_from_line_reports_framing_damage_as_empty(self):
        self.assertEqual(gemini._extract_texts_from_line("not a frame"), [])


if __name__ == "__main__":
    unittest.main()
