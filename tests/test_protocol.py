"""Protocol framing, record extraction and candidate parsing."""
import json
import unittest

from gemini_web2api.protocol import (BardError, FrameDecoder, ProtocolFrameError,
                                     collect_candidates, decode_frame, iter_records,
                                     parse_candidate, parse_payload,
                                     parse_response_text, record_error_code,
                                     record_payload)

XSSI = ")]}'"


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def wire_frame(records, hint_override=None) -> str:
    body = json.dumps(records, separators=(",", ":"), ensure_ascii=False)
    units = utf16_units(body) if hint_override is None else hint_override
    return f"{units}\n{body}\n"


def wire_record(payload) -> str:
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return wire_frame([["wrb.fr", None, payload_json]])


def wire_stream(payload, terminate=True) -> str:
    stream = XSSI + "\n\n" + wire_record(payload)
    if terminate:
        stream += wire_frame([["e", 4, None, None, 1]])
    return stream


def make_payload(text="hello", thoughts=None, final=False, rcid="rc_1",
                 generated=None, generated_img2img=None, web_images=None):
    candidate = [None] * 40
    candidate[0] = rcid
    candidate[1] = [text]
    candidate[8] = [2] if final else [1]
    if thoughts:
        candidate[37] = [[thoughts]]
    media = [None] * 8
    if web_images:
        media[1] = [[[[url], None, None, None, alt]] for url, alt in web_images]
    if generated:
        media[7] = [[[[None, None, None, [None, None, alt, url]], [image_id]]]
                    for url, alt, image_id in generated]
    if generated_img2img:
        media[0] = {"8": [[[[None, None, None, [None, None, alt, url]], [image_id]]]
                         for url, alt, image_id in generated_img2img]}
    candidate[12] = media
    return [None] * 4 + [[candidate]]


def bard_error_payload(code):
    status = [None, None, [[None, [code]]]]
    return [["wrb.fr", None, json.dumps(make_payload("blocked"), separators=(",", ":")), None,
             None, status]]


class FrameDecoderTests(unittest.TestCase):
    def decode(self, chunks):
        decoder = FrameDecoder()
        records = []
        for chunk in chunks:
            records.extend(decoder.feed(chunk))
        records.extend(decoder.close())
        return records

    def test_decodes_prefix_hint_and_records(self):
        stream = wire_stream(make_payload("hi"))
        records = self.decode([stream])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0][0], "wrb.fr")
        self.assertEqual(records[1][0], "e")
        self.assertEqual(parse_payload(record_payload(records[0]))[0].text, "hi")

    def test_record_split_across_many_chunks(self):
        stream = wire_stream(make_payload("split me"))
        chunks = [stream[i:i + 3] for i in range(0, len(stream), 3)]
        records = self.decode(chunks)
        self.assertEqual(len(records), 2)
        self.assertEqual(parse_response_text(stream), "split me")

    def test_multiple_frames_glued_into_one_chunk(self):
        stream = wire_stream(make_payload("first")) + wire_record(make_payload("second"))
        records = self.decode([stream])
        self.assertEqual(len(records), 3)
        texts = [parse_payload(record_payload(r))[0].text for r in records if r[0] == "wrb.fr"]
        self.assertEqual(texts, ["first", "second"])

    def test_wrong_length_hint_falls_back_to_line_scan(self):
        # A hint that under-counts would silently eat the next frame; the decoder
        # must recover from the damage instead of dropping the answer.
        stream = XSSI + "\n\n" + wire_frame([["wrb.fr", None, json.dumps(make_payload("recover"))]],
                                            hint_override=5)
        records = self.decode([stream])
        self.assertTrue(any(r[0] == "wrb.fr" for r in records), records)
        self.assertEqual(parse_response_text(stream), "recover")

    def test_utf16_code_units_count_surrogates_as_two(self):
        text = "a\U0001F600b"          # emoji is one Python char, two UTF-16 units
        stream = wire_stream(make_payload(text))
        self.assertEqual(parse_response_text(stream), text)
        # And the hint really is the UTF-16 length, not the character count.
        payload_json = json.dumps(make_payload(text), separators=(",", ":"), ensure_ascii=False)
        frame_body = json.dumps([["wrb.fr", None, payload_json]], separators=(",", ":"),
                                ensure_ascii=False)
        self.assertEqual(utf16_units(frame_body), len(frame_body.encode("utf-16-le")) // 2)
        self.assertGreater(utf16_units(frame_body), len(frame_body))

    def test_accepts_stream_without_xssi_prefix(self):
        stream = wire_record(make_payload("no prefix"))
        self.assertEqual(parse_response_text(stream), "no prefix")

    def test_close_flushes_trailing_unterminated_record(self):
        stream = wire_stream(make_payload("trailing"), terminate=False).rstrip("\n")
        self.assertEqual(parse_response_text(stream), "trailing")

    def test_oversized_frame_is_rejected(self):
        decoder = FrameDecoder(max_frame_bytes=64)
        with self.assertRaises(ProtocolFrameError):
            decoder.feed("x" * 200)

    def test_decode_frame_reports_damage(self):
        with self.assertRaises(ProtocolFrameError):
            decode_frame("this is not json")

    def test_decode_frame_recovers_glued_segments(self):
        glued = json.dumps([["a"]]).replace("]]", "]]\n") + json.dumps([["b"]])
        self.assertEqual(decode_frame(glued), [["a"], ["b"]])

    def test_iter_records_ignores_pure_noise(self):
        self.assertEqual(iter_records(XSSI + "\n\n"), [])
        self.assertEqual(iter_records(""), [])


class CandidateParsingTests(unittest.TestCase):
    def parse_first(self, payload):
        return parse_payload(payload)[0]

    def test_text_uses_first_run(self):
        payload = make_payload(text="the answer")
        self.assertEqual(self.parse_first(payload).text, "the answer")

    def test_text_falls_back_to_longest_run_when_head_missing(self):
        candidate = [None] * 40
        candidate[1] = [None, "short", "a much longer run"]
        payload = [None] * 4 + [[candidate]]
        self.assertEqual(self.parse_first(payload).text, "a much longer run")

    def test_text_ignores_short_payload_threshold(self):
        # The old decoder dropped frames shorter than 200 bytes / 50 chars, which
        # could discard a perfectly good one-word answer.
        payload = make_payload(text="Ok")
        self.assertEqual(self.parse_first(payload).text, "Ok")

    def test_thoughts_and_completion_flag(self):
        payload = make_payload(text="done", thoughts="step 1\nstep 2", final=True)
        candidate = self.parse_first(payload)
        self.assertEqual(candidate.thoughts, "step 1\nstep 2")
        self.assertTrue(candidate.is_final)

        self.assertFalse(self.parse_first(make_payload("x")).is_final)

    def test_generated_images_plain_and_img2img(self):
        plain = self.parse_first(make_payload(
            generated=[("https://gen/1.png", "alt one", "img-1")]))
        self.assertEqual(len(plain.generated_images), 1)
        self.assertEqual(plain.generated_images[0]["url"], "https://gen/1.png")
        self.assertEqual(plain.generated_images[0]["alt"], "alt one")
        self.assertEqual(plain.generated_images[0]["image_id"], "img-1")

        img2img = self.parse_first(make_payload(
            generated_img2img=[("https://gen/2.png", "alt two", "img-2")]))
        self.assertEqual(img2img.generated_images[0]["url"], "https://gen/2.png")

    def test_generated_image_without_id_gets_namespaced_default(self):
        image = self.parse_first(make_payload(generated=[("https://gen/3.png", "", "")]))
        self.assertTrue(image.generated_images[0]["image_id"].startswith(
            "http://googleusercontent.com/image_generation_content/"))

    def test_web_images_are_parsed(self):
        candidate = self.parse_first(make_payload(
            web_images=[("https://site/a.png", "site alt")]))
        self.assertEqual(candidate.web_images[0]["url"], "https://site/a.png")
        self.assertEqual(candidate.web_images[0]["alt"], "site alt")

    def test_card_content_placeholder_prefers_card_text(self):
        candidate = [None] * 40
        candidate[1] = ["http://googleusercontent.com/card_content/7"]
        candidate[12] = None
        candidate[22] = ["real answer"]
        payload = [None] * 4 + [[candidate]]
        self.assertEqual(self.parse_first(payload).text, "real answer")

    def test_empty_candidate_is_skipped(self):
        self.assertEqual(parse_payload([None] * 4 + [[[None] * 40]]), [])

    def test_payload_without_candidate_container(self):
        self.assertEqual(parse_payload([1, 2, 3]), [])

    def test_web_images_rejects_entries_without_url(self):
        media = [None] * 8
        media[1] = [[[[]]]]
        candidate = [None] * 40
        candidate[1] = ["text"]
        candidate[12] = media
        payload = [None] * 4 + [[candidate]]
        self.assertEqual(self.parse_first(payload).web_images, [])


class BardErrorTests(unittest.TestCase):
    def test_error_code_extraction(self):
        record = bard_error_payload(1095)
        self.assertEqual(record_error_code(record[0]), 1095)

    def test_non_error_record_has_no_code(self):
        self.assertIsNone(record_error_code(["wrb.fr", None, "{}"]))

    def test_error_code_maps_to_status_and_retryability(self):
        quota = BardError(1095)
        self.assertEqual(quota.http_status, 429)
        self.assertTrue(quota.retryable)

        header = BardError(1052)
        self.assertFalse(header.retryable)

        self.assertEqual(BardError(1060).http_status, 403)
        self.assertEqual(BardError(1037).http_status, 429)
        self.assertEqual(BardError(9999).http_status, 502)

    def test_parse_response_text_raises_on_error_record(self):
        stream = XSSI + "\n\n" + wire_frame(bard_error_payload(1037))
        with self.assertRaises(BardError) as context:
            parse_response_text(stream)
        self.assertEqual(context.exception.code, 1037)

    def test_parse_response_text_raises_on_legacy_plain_text_marker(self):
        with self.assertRaises(BardError) as context:
            parse_response_text("BardErrorInfo [1050]")
        self.assertEqual(context.exception.code, 1050)

    def test_collect_candidates_returns_every_candidate(self):
        payload = [None] * 4 + [[[None] * 40, [None] * 40]]
        payload[4][0][1] = ["first"]
        payload[4][1][1] = ["second"]
        stream = XSSI + "\n\n" + wire_record(payload)
        texts = [candidate.text for candidate in collect_candidates(stream)]
        self.assertEqual(texts, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
