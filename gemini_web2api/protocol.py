"""Gemini Web wire protocol: XSSI framing, record decoding and candidate parsing.

The upstream body is *not* line-delimited JSON. A real StreamGenerate response
looks like this:

    )]}'

    934
    [["wrb.fr",null,"<payload json string>"]]
    27
    [["e",4,null,null,1]]

The decimal lines are length hints measured in UTF-16 code units. One record can
span several network chunks, and several records can be glued into a single
chunk, so the previous "split on \\n and ignore lines shorter than 200 bytes"
approach silently dropped short frames and could mangle long ones.

`FrameDecoder` consumes the stream incrementally, honours the length hints, and
falls back to a per-line scan whenever a hint disagrees with the payload (Google
has shipped off-by-a-few hints before).

`parse_payload` maps a decoded payload onto `Candidate` objects following the
field layout used by the web client (see docs in README):
    candidate[1]     text runs            -> text
    candidate[8][0]  completion indicator -> is_final (2 == finished)
    candidate[12][1] web image references -> web_images
    candidate[12][7] generated images     -> generated_images (plain generation)
    candidate[12][0]["8"]                 -> generated_images (image to image)
    candidate[37][0][0]                   -> thoughts
"""
import json
import re
from dataclasses import dataclass, field

# A single record is capped so a hostile/broken upstream cannot exhaust memory.
MAX_PROTOCOL_FRAME_BYTES = 64 * 1024 * 1024
XSSI_PREFIX = ")]}'"
_UTF16_SURROGATE_THRESHOLD = 0xFFFF

_CARD_CONTENT_RE = re.compile(r"^http://googleusercontent\.com/card_content/\d+")
_DIGITS_RE = re.compile(r"^\d+$")

# BardErrorInfo codes. They arrive inside record[5] as
# [null,null,[[null,[code]]]] and describe an upstream refusal, not transport.
_BARD_ERROR_CODES = {
    1037: (429, True, "Gemini Web 该账号的当前模型用量已达上限"),
    1050: (400, False, "Gemini Web 模型与会话历史不一致"),
    1052: (502, False, "Gemini Web 拒绝了模型请求头，模型暂不可用或协议已变化"),
    1060: (403, False, "Google 暂时限制了当前出口 IP"),
    1095: (429, True, "Gemini Web 暂时限制了该账号的请求频率"),
    1013: (502, True, "Gemini Web 上游临时错误，稍后重试通常可恢复"),
}


class ProtocolFrameError(ValueError):
    """A frame could not be decoded at all (framing/JSON damage)."""


class BardError(RuntimeError):
    """Upstream answered with a BardErrorInfo refusal."""

    def __init__(self, code, message=None, http_status=None, retryable=None):
        self.code = code
        status, retryable_default, default_message = _BARD_ERROR_CODES.get(
            code, (502, True, f"Gemini Web 返回协议错误码 {code}"))
        super().__init__(message or default_message)
        self.http_status = http_status or status
        # None means "use the table"; pass an explicit bool to override.
        self.retryable = retryable_default if retryable is None else retryable


@dataclass
class Candidate:
    """One reply candidate from a StreamGenerate payload."""

    rcid: str = ""
    text: str = ""
    thoughts: str = ""
    web_images: list = field(default_factory=list)
    generated_images: list = field(default_factory=list)
    is_final: bool = False

    def __bool__(self):
        return bool(self.text or self.thoughts or self.generated_images)


# ─── field access helpers ────────────────────────────────────────────────────


def _at(container, index, default=None):
    """Read `index` from a list, or the string form of it from a dict.

    Google uses mixed arrays/objects in the same position depending on the
    feature, e.g. candidate[12][0] is a list in some frames and {"8": [...]} in
    others, so both shapes have to be accepted.
    """
    if container is None:
        return default
    if isinstance(container, dict):
        return container.get(str(index), default)
    if isinstance(container, list):
        if index < 0 or index >= len(container):
            return default
        return container[index]
    return default


def _nested(container, path, default=None):
    current = container
    for step in path:
        current = _at(current, step, default)
        if current is default:
            return default
    return current


def _first_string(container):
    """Deepest-search a nested structure for the first non-empty string."""
    if isinstance(container, str):
        return container
    if isinstance(container, list):
        for item in container:
            found = _first_string(item)
            if found:
                return found
    if isinstance(container, dict):
        for key in container:
            found = _first_string(container[key])
            if found:
                return found
    return ""


# ─── incremental frame decoding ──────────────────────────────────────────────


class FrameDecoder:
    """Incrementally decode the `)]}'` + length-prefix framing.

    Usage:
        decoder = FrameDecoder()
        for chunk in response_chunks:
            for record in decoder.feed(chunk):
                handle(record)
        for record in decoder.close():
            handle(record)
    """

    def __init__(self, max_frame_bytes=MAX_PROTOCOL_FRAME_BYTES):
        self.max_frame_bytes = max_frame_bytes
        self._buffer = ""
        self._pending_units = None
        self._saw_prefix = False
        self._closed = False

    # -- public API ----------------------------------------------------------

    def feed(self, chunk):
        """Consume a text chunk and return every record that is now complete."""
        if self._closed:
            return []
        if chunk:
            self._buffer += chunk
        if len(self._buffer) > self.max_frame_bytes:
            raise ProtocolFrameError(
                f"protocol frame exceeds {self.max_frame_bytes} bytes")
        records = []
        while True:
            progressed, batch = self._drain(final=False)
            records.extend(batch)
            if not progressed:
                break
        return records

    def close(self):
        """Flush the remaining buffer; returns any trailing records."""
        if self._closed:
            return []
        self._closed = True
        records = []
        while True:
            progressed, batch = self._drain(final=True)
            records.extend(batch)
            if not progressed:
                break
        leftover = self._buffer.strip()
        self._buffer = ""
        if leftover and leftover != XSSI_PREFIX:
            # Trailing garbage that never formed a frame: parse what we can
            # rather than dropping the answer on the floor.
            records.extend(_records_from_garbage(leftover))
        return records

    # -- internals -----------------------------------------------------------

    def _drain(self, final):
        """Consume one unit of buffered input. Returns (progressed, records)."""
        buffer = self._buffer
        if not buffer:
            return False, []

        if not self._saw_prefix:
            if XSSI_PREFIX.startswith(buffer):
                # Not enough data yet to tell whether the prefix is present.
                return False, []
            if buffer.startswith(XSSI_PREFIX):
                buffer = buffer[len(XSSI_PREFIX):]
            self._saw_prefix = True
            self._buffer = buffer
            return True, []

        if self._pending_units is not None:
            # A pending length hint means the next `n` UTF-16 code units are a
            # record body.
            text, consumed, complete = _take_units(buffer, self._pending_units)
            if not complete and not final:
                return False, []
            try:
                records = _parse_frame_records(text)
            except ProtocolFrameError:
                # The hint disagrees with the payload (Google has shipped
                # off-by-a-few hints). Drop the hint and re-scan the same bytes
                # line by line instead of losing the frame.
                self._pending_units = None
                return self._drain_line(final)
            self._pending_units = None
            self._buffer = buffer[consumed:]
            return True, records

        return self._drain_line(final)

    def _drain_line(self, final):
        """Line-oriented fallback path: length hints are ignored here."""
        buffer = self._buffer
        newline = buffer.find("\n")
        if newline < 0:
            if not final:
                return False, []
            line, rest = buffer, ""
        else:
            line, rest = buffer[:newline + 1], buffer[newline + 1:]

        stripped = line.strip()
        if not stripped:
            self._buffer = rest
            return True, []

        if _DIGITS_RE.match(stripped):
            self._buffer = rest
            self._pending_units = int(stripped)
            return True, []

        self._buffer = rest
        try:
            return True, _parse_frame_records(stripped)
        except ProtocolFrameError:
            if final:
                return True, _records_from_garbage(stripped)
            raise


def _take_units(buffer, units):
    """Split off the first `units` UTF-16 code units of `buffer`.

    Returns (text, consumed_chars, complete). Counting UTF-16 code units (not
    Python characters) matters: an emoji or a CJK extension character counts as
    two units, so a naive len() slice drifts and desynchronises the stream.
    """
    consumed = 0
    count = 0
    length = len(buffer)
    while consumed < length and count < units:
        char = buffer[consumed]
        code = ord(char)
        width = 1
        if code > _UTF16_SURROGATE_THRESHOLD:
            width = 2
        if count + width > units:
            break
        count += width
        consumed += 1
    return buffer[:consumed], consumed, count >= units


def _parse_frame_records(text):
    """Decode one frame body into its records (empty list when nothing there)."""
    text = text.strip()
    if not text or text == XSSI_PREFIX:
        return []
    return decode_frame(text)


def decode_frame(text):
    """Decode one frame into a flat list of records.

    Primary path is a single json.loads. When that fails the text is split again
    and each parseable segment is merged, which recovers responses where several
    frames were glued together or a length hint was wrong.
    """
    try:
        frame = json.loads(text)
    except ValueError as error:
        recovered = _records_from_garbage(text)
        if recovered:
            return recovered
        raise ProtocolFrameError(f"decode protocol frame: {error}") from error
    if isinstance(frame, list):
        return frame
    return [frame]


def _records_from_garbage(text):
    """Best-effort record recovery from damaged/glued frame text."""
    records = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line == XSSI_PREFIX or _DIGITS_RE.match(line):
            continue
        try:
            segment = json.loads(line)
        except ValueError:
            continue
        if isinstance(segment, list):
            records.extend(segment)
    return records


def iter_records(text):
    """Decode a complete (non-streamed) body into records."""
    decoder = FrameDecoder()
    records = decoder.feed(text)
    records.extend(decoder.close())
    return records


# ─── candidate parsing ───────────────────────────────────────────────────────


def record_error_code(record):
    """Read the BardErrorInfo code out of `wrb.fr`[5], or None."""
    status = _at(record, 5)
    if not isinstance(status, list):
        return None
    details = _at(status, 2)
    detail = _at(details, 0)
    codes = _at(detail, 1)
    code = _at(codes, 0)
    if isinstance(code, int):
        return code
    return None


def parse_payload(payload):
    """Map a decoded StreamGenerate payload onto Candidate objects."""
    container = _at(payload, 4)
    if not isinstance(container, list):
        return []
    candidates = []
    for raw in container:
        if not isinstance(raw, (list, dict)):
            continue
        candidate = parse_candidate(raw)
        if candidate:
            candidates.append(candidate)
    return candidates


def parse_candidate(raw):
    """Parse a single candidate array. Returns None when it holds no content."""
    text = _candidate_text(raw)
    thoughts = _first_string(_nested(raw, [37, 0], "")) or ""
    card = _nested(raw, [22, 0], "")
    if card and (not text or _CARD_CONTENT_RE.match(text)):
        # Some answers are delivered as a card placeholder; the real text sits
        # in the card slot.
        text = _first_string(card) or text
    indicator = _nested(raw, [8, 0])
    candidate = Candidate(
        rcid=_first_string(_at(raw, 0, "")) or "",
        text=text,
        thoughts=thoughts,
        web_images=_parse_web_images(raw),
        generated_images=_parse_generated_images(raw),
        is_final=(indicator == 2),
    )
    if not candidate:
        return None
    return candidate


def _candidate_text(raw):
    """Extract the answer text.

    candidate[1][0] is the authoritative full text. It is a list of text runs in
    the live protocol, so the head is used when present and the longest run is
    used as a fallback for older shapes.
    """
    head = _at(_at(raw, 1), 0)
    if isinstance(head, str) and head:
        return head
    runs = _at(raw, 1)
    if isinstance(runs, str):
        return runs
    if isinstance(runs, list):
        strings = [item for item in runs if isinstance(item, str) and item]
        if strings:
            return max(strings, key=len)
    return ""


def _parse_web_images(raw):
    images = []
    for entry in _nested(raw, [12, 1], []) or []:
        url = _first_string(_nested(entry, [0, 0, 0], None)) or ""
        if not url:
            continue
        images.append({"url": url, "alt": _first_string(_nested(entry, [0, 4], "")) or "",
                       "source": "web"})
    return images


def _parse_generated_images(raw):
    entries = []
    plain = _nested(raw, [12, 7, 0], [])
    if isinstance(plain, list):
        entries.extend(plain)
    img2img = _nested(raw, [12, 0, 8, 0], [])
    if isinstance(img2img, list):
        entries.extend(img2img)
    images = []
    for index, entry in enumerate(entries):
        url = _first_string(_nested(entry, [0, 3, 3], None)) or ""
        if not url:
            continue
        image_id = _first_string(_nested(entry, [1, 0], "")) or ""
        images.append({
            "url": url,
            "alt": _first_string(_nested(entry, [0, 3, 2], "")) or "",
            "image_id": image_id or f"http://googleusercontent.com/image_generation_content/{index}",
            "source": "generated",
        })
    return images


def parse_response_text(raw):
    """Non-streaming helper: the longest answer text in a whole body."""
    best = ""
    for record in iter_records(raw):
        code = record_error_code(record)
        if code is not None:
            raise BardError(code)
        payload = _payload(record)
        if payload is None:
            continue
        for candidate in parse_payload(payload):
            if len(candidate.text) > len(best):
                best = candidate.text
    if not best:
        # Last-resort guard: older upstream shapes embed the refusal in plain
        # text instead of a structured record.
        match = re.search(r"BardErrorInfo\s*\[(\d+)\]", raw)
        if match:
            raise BardError(int(match.group(1)))
    return best


def collect_candidates(raw):
    """Non-streaming helper returning every parsed candidate of a body."""
    candidates = []
    for record in iter_records(raw):
        code = record_error_code(record)
        if code is not None:
            raise BardError(code)
        payload = _payload(record)
        if payload is None:
            continue
        candidates.extend(parse_payload(payload))
    return candidates


def _payload(record):
    """Decode the embedded JSON payload of a `wrb.fr` record."""
    if not isinstance(record, list):
        return None
    if _first_string(_at(record, 0, "")) != "wrb.fr":
        return None
    encoded = _at(record, 2)
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        return json.loads(encoded)
    except ValueError:
        return None


def record_payload(record):
    """Public alias of the `wrb.fr` payload accessor."""
    return _payload(record)
