"""Gemini StreamGenerate protocol client.

Responsibilities:
  * build the request payload and headers for one Google account
  * fail over between accounts on credential-level errors
  * renew cookies when Google rejects them (see rotation.py)
  * decode the framed response stream (see protocol.py) and expose text
    deltas, optional reasoning deltas, and generated media
  * abort a stalled upstream stream so a thread is never parked forever
"""
import hashlib
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass, field

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from . import rotation
from .config import (CONFIG, account_cookie_path, get_accounts, get_int,
                     resolve_path)
from .protocol import (BardError, FrameDecoder, ProtocolFrameError,
                       parse_payload, record_error_code, record_payload)

_ssl_ctx = None
_ssl_lock = threading.Lock()

_cookie_lock = threading.Lock()
_cookie_cache = {}      # cookie file path -> {"str", "sapisid", "mtime"}
_COOKIE_CACHE_MAX = 64

_httpx_clients = {}     # (proxy, timeout, stream) -> httpx.Client
_httpx_lock = threading.Lock()
_HTTPX_CLIENT_MAX = 8

LOG_BUFFER_SIZE = 500
_log_buffer = deque(maxlen=LOG_BUFFER_SIZE)
_log_lock = threading.Lock()
_log_seq = 0
_ERROR_HINTS = ("error", "failed", "retry", "barderrorinfo", "rejected",
                "traceback", "falling back", "timed out")
_LOG_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40}

# Status codes that mean "this account cannot serve the request" and justify
# moving to the next one: bad/expired credentials, missing permission, quota.
_ACCOUNT_FAIL_CODES = (400, 401, 403, 429)
# Codes worth a cookie renewal attempt before switching accounts.
_CREDENTIAL_FAIL_CODES = (400, 401, 403)

# MODE_CATEGORY enum from the Gemini frontend JS.
_MODE_NAMES = {1: "FAST", 2: "THINKING", 3: "PRO", 4: "AUTO",
               5: "FAST_DYN", 6: "FLASH_LITE"}


# ─── logging ─────────────────────────────────────────────────────────────────


def log(msg: str, level: str = None):
    """Record a log line in the in-memory ring buffer (for /ui) and stderr."""
    global _log_seq
    lowered = msg.lower()
    resolved = level or ("error" if any(h in lowered for h in _ERROR_HINTS) else "info")
    with _log_lock:
        _log_seq += 1
        seq = _log_seq
        _log_buffer.append({"seq": seq, "t": time.strftime('%H:%M:%S'),
                            "level": resolved, "msg": msg})
    if CONFIG.get("log_requests"):
        threshold = _LOG_LEVELS.get(str(CONFIG.get("log_level", "info")).lower(), 20)
        if _LOG_LEVELS.get(resolved, 20) >= threshold:
            sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
            sys.stderr.flush()


def get_logs(since: int = 0) -> list:
    """Return buffered log entries newer than `since`, plus the latest seq."""
    with _log_lock:
        return [e for e in _log_buffer if e["seq"] > since]


def latest_log_seq() -> int:
    with _log_lock:
        if _log_seq:
            return _log_seq
        return _log_buffer[-1]["seq"] if _log_buffer else 0


def clear_logs():
    with _log_lock:
        _log_buffer.clear()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        with _ssl_lock:
            if _ssl_ctx is None:
                _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


# ─── HTTP clients ────────────────────────────────────────────────────────────


def _httpx_client(stream: bool = False):
    """Return a pooled httpx client for the current proxy settings.

    The streaming client uses the read timeout as a stall watchdog: httpx applies
    it between reads, so a connection that goes silent is aborted instead of
    holding the worker thread until the OS gives up.
    """
    if not HAS_HTTPX:
        return None
    proxy = CONFIG.get("proxy") or ""
    stall = get_int("stream_stall_timeout_sec", 120)
    timeout = (httpx.Timeout(connect=15.0, read=(float(stall) if stall > 0 else None),
                             write=60.0, pool=15.0)
               if stream else float(get_int("request_timeout_sec", 180)))
    key = (proxy, str(timeout), stream)
    with _httpx_lock:
        client = _httpx_clients.get(key)
        if client is not None:
            return client
        transport = httpx.HTTPTransport(proxy=proxy or None) if proxy else None
        client = httpx.Client(transport=transport, timeout=timeout, verify=True,
                              follow_redirects=False)
        if len(_httpx_clients) >= _HTTPX_CLIENT_MAX:
            for stale_key, stale in list(_httpx_clients.items()):
                if stale_key == key:
                    continue
                try:
                    stale.close()
                except Exception:
                    pass
                _httpx_clients.pop(stale_key, None)
                break
        _httpx_clients[key] = client
        return client


def close_http_clients():
    """Close every pooled client (used on shutdown and by tests)."""
    with _httpx_lock:
        clients = list(_httpx_clients.values())
        _httpx_clients.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


def _opener(proxy=None):
    handlers = [
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else None,
        urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
    ]
    return urllib.request.build_opener(*[h for h in handlers if h is not None])


# ─── credentials ─────────────────────────────────────────────────────────────


def cookie_file_for(auth_user=None) -> str:
    """Absolute path of the cookie file backing one account ('' when unset)."""
    return resolve_path(account_cookie_path(auth_user)) or ""


def load_cookie(auth_user=None) -> tuple:
    """Load one account's cookie from file, with mtime-based caching.

    auth_user=None means the active account. Each account keeps its own cache
    entry keyed by file path, so switching accounts never serves stale data and
    a rotated cookie file is picked up as soon as its mtime changes.
    """
    cookie_file = cookie_file_for(auth_user)
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        with _cookie_lock:
            cached = _cookie_cache.get(cookie_file)
        if cached and cached["mtime"] == mtime and cached["str"]:
            return cached["str"], cached["sapisid"]
        with open(cookie_file, "r", encoding="utf-8") as handle:
            content = handle.read().strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = data.get("cookie", "")
            sapisid = data.get("sapisid", "")
        else:
            cookie_str = content
            pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
            sapisid = pairs.get("SAPISID", "")
        # Defensive: files written by older builds (or hand-edited) may still
        # contain newlines, which urllib rejects as an invalid header value.
        cookie_str = " ".join(cookie_str.split())
        if sapisid:
            sapisid = "".join(sapisid.split())
        with _cookie_lock:
            if len(_cookie_cache) >= _COOKIE_CACHE_MAX:
                _cookie_cache.clear()
            _cookie_cache[cookie_file] = {"str": cookie_str, "sapisid": sapisid or None,
                                         "mtime": mtime}
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        with _cookie_lock:
            cached = _cookie_cache.get(cookie_file) or {}
        return cached.get("str", ""), cached.get("sapisid")


def forget_cookie_cache(path: str = None):
    """Drop cached credentials so the next read hits the file."""
    with _cookie_lock:
        if path:
            _cookie_cache.pop(path, None)
        else:
            _cookie_cache.clear()


def make_sapisidhash(sapisid: str) -> str:
    ts = int(time.time())
    h = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{h}"


def _account_prefix(auth_user=None) -> str:
    """Return the Gemini account path prefix for non-default Google accounts."""
    if auth_user is None or auth_user == "":
        return ""
    return f"/u/{auth_user}"


def _build_headers(auth_user=None, cookie_str=None, sapisid=None) -> dict:
    account_prefix = _account_prefix(auth_user)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{account_prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_prefix:
        headers["X-Goog-AuthUser"] = str(auth_user)
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    return headers


# ─── payload ─────────────────────────────────────────────────────────────────


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                   extra_fields: dict = None, xsrf_token: str = None) -> str:
    inner = [None] * 102
    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]
    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _apply_chat_persistence_flags(inner)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id
    if extra_fields:
        for k, v in extra_fields.items():
            inner[k] = v
    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if xsrf_token:
        params["at"] = xsrf_token
    return urllib.parse.urlencode(params)


def _get_url(auth_user=None) -> str:
    reqid = int(time.time()) % 1000000
    account_prefix = _account_prefix(auth_user)
    return (
        f"https://gemini.google.com{account_prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={CONFIG['gemini_bl']}&hl=en&_reqid={reqid}&rt=c"
    )


def fetch_latest_bl() -> str:
    """Fetch the current bard-web-server build label from gemini.google.com."""
    try:
        request = urllib.request.Request(
            "https://gemini.google.com/app",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        opener = _opener(CONFIG.get("proxy"))
        response = opener.open(request, timeout=15)
        try:
            html = response.read().decode("utf-8", errors="replace")
        finally:
            response.close()
        match = re.search(r'(boq_assistant-bard-web-server_\d+\.\d+_p\d+)', html)
        if match:
            return match.group(1)
    except Exception as e:
        log(f"BL auto-update fetch failed: {e}")
    return None


def update_bl_if_needed() -> bool:
    """Fetch and update gemini_bl when Google rotates the frontend version."""
    new_bl = fetch_latest_bl()
    if new_bl and new_bl != CONFIG["gemini_bl"]:
        log(f"BL auto-updated: {CONFIG['gemini_bl']} -> {new_bl}")
        CONFIG["gemini_bl"] = new_bl
        return True
    return False


# ─── text hygiene ────────────────────────────────────────────────────────────


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


# ─── accounts ────────────────────────────────────────────────────────────────


def _account_order() -> list:
    """Enabled account indices, starting from the active one.

    Returns [None] when no accounts are configured, meaning "use the legacy
    single-value config" (which also covers anonymous mode).
    """
    accts = get_accounts()
    if not accts:
        return [None]
    start = CONFIG.get("active_account") or 0
    order = []
    for i in range(len(accts)):
        idx = (start + i) % len(accts)
        if accts[idx].get("enabled", True):
            order.append(idx)
    return order or [None]


def _account_ctx(idx):
    """(auth_user, cookie_str, sapisid, xsrf_token) for one account.

    idx=None falls back to the legacy single-value config.
    """
    if idx is None:
        cookie_str, sapisid = load_cookie()
        return CONFIG.get("auth_user"), cookie_str, sapisid, CONFIG.get("xsrf_token")
    account = get_accounts()[idx]
    auth_user = account.get("auth_user")
    cookie_str, sapisid = load_cookie(auth_user)
    return auth_user, cookie_str, sapisid, account.get("xsrf_token")


def _acct_label(idx) -> str:
    if idx is None:
        return "default (no account configured)"
    account = get_accounts()[idx]
    auth_user = account.get("auth_user")
    return f"/u/{auth_user}" if auth_user is not None and auth_user != "" else f"account#{idx}"


def _http_status(e) -> int:
    """HTTP status carried by an exception (urllib, httpx or BardError), else 0."""
    code = getattr(e, "http_status", None) or getattr(e, "code", 0)
    if code:
        return int(code)
    return int(getattr(getattr(e, "response", None), "status_code", 0) or 0)


def _is_account_error(e) -> bool:
    """True when the failure is about the account, not the network.

    ValueError covers a malformed credential (e.g. a cookie containing a
    newline), which urllib rejects before the request is ever sent. Framing
    damage also raises a ValueError subclass, so it is excluded explicitly.
    """
    if isinstance(e, ProtocolFrameError):
        return False
    if isinstance(e, BardError):
        # Only the refusal codes that another account can actually fix.
        return e.code in (1037, 1060, 1095)
    return _http_status(e) in _ACCOUNT_FAIL_CODES or isinstance(e, ValueError)


def _is_credential_error(e) -> bool:
    """Renewal only helps when Google rejected the cookies themselves."""
    if isinstance(e, (ProtocolFrameError, BardError)):
        return False
    return _http_status(e) in _CREDENTIAL_FAIL_CODES or isinstance(e, ValueError)


def _renew_account_cookies(idx, error) -> bool:
    """Rotate the account's cookies and report whether new ones were stored."""
    if not CONFIG.get("cookie_rotation", True):
        return False
    if not _is_credential_error(error):
        return False
    auth_user = None
    if idx is not None:
        auth_user = get_accounts()[idx].get("auth_user")
    path = cookie_file_for(auth_user)
    if not path:
        log("Cookie rotation skipped: no cookie file configured for this account")
        return False
    log(f"Account {_acct_label(idx)} rejected the request; attempting cookie rotation")
    renewed = rotation.renew_cookie_file(
        path,
        proxy=CONFIG.get("proxy"),
        timeout=min(30, max(5, get_int("image_fetch_timeout_sec", 30))),
        ssl_ctx=_get_ssl_ctx(),
        min_interval=get_int("cookie_rotation_min_interval_sec", 60),
        log=log,
    )
    if renewed:
        forget_cookie_cache(path)
        log(f"Account {_acct_label(idx)} cookies renewed; retrying with fresh credentials")
    return renewed


# ─── results ─────────────────────────────────────────────────────────────────


@dataclass
class GenerationResult:
    """Everything one upstream call produced for a single prompt."""

    text: str = ""
    thoughts: str = ""
    web_images: list = field(default_factory=list)
    generated_images: list = field(default_factory=list)

    @property
    def images(self) -> list:
        return self.web_images + self.generated_images


def _merge_result(result: GenerationResult, candidates) -> GenerationResult:
    """Fold candidate fields into the best result seen so far."""
    for candidate in candidates:
        if len(candidate.text or "") > len(result.text or ""):
            result.text = candidate.text
        if len(candidate.thoughts or "") > len(result.thoughts or ""):
            result.thoughts = candidate.thoughts
        for image in candidate.web_images:
            if image not in result.web_images:
                result.web_images.append(image)
        for image in candidate.generated_images:
            if image not in result.generated_images:
                result.generated_images.append(image)
    return result


def extract_response_text(raw: str) -> str:
    """Non-streaming helper: return the cleaned final answer text."""
    result = parse_result(raw)
    return clean_text(result.text)


def parse_result(raw: str) -> GenerationResult:
    """Decode a complete (buffered) upstream body into a GenerationResult."""
    result = GenerationResult()
    for record in _records_of(raw):
        _raise_for_record_error(record)
        payload = record_payload(record)
        if payload is None:
            continue
        _merge_result(result, parse_payload(payload))
    if not result.text:
        # Last-resort guard: some upstream shapes embed the refusal in plain text
        # instead of a structured record.
        match = re.search(r"BardErrorInfo\s*\[(\d+)\]", raw)
        if match:
            raise BardError(int(match.group(1)))
    return result


def _records_of(raw: str):
    decoder = FrameDecoder()
    records = decoder.feed(raw)
    records.extend(decoder.close())
    return records


def _raise_for_record_error(record):
    code = record_error_code(record)
    if code is not None:
        raise BardError(code)


def _extract_texts_from_line(line: str) -> list:
    """Backwards-compatible helper: texts carried by one frame line."""
    try:
        records = _records_of(line)
    except ProtocolFrameError:
        return []
    texts = []
    for record in records:
        payload = record_payload(record)
        if payload is None:
            continue
        for candidate in parse_payload(payload):
            if candidate.text:
                texts.append(candidate.text)
    return texts


# ─── generation ──────────────────────────────────────────────────────────────


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
             extra_fields: dict = None, cookie_str: str = None,
             sapisid: str = None, capture: list = None) -> str:
    """Non-streaming generation returning only the answer text.

    `capture` is an optional out-parameter: when a list is passed, the
    GenerationResult of the successful call is appended to it. The HTTP layer
    uses it to expose reasoning and generated media without changing the
    long-standing "generate returns a string" contract.
    """
    return generate_detailed(prompt, model_id, think_mode, file_refs, extra_fields,
                             cookie_str, sapisid, capture).text


def generate_detailed(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                      extra_fields: dict = None, cookie_str: str = None,
                      sapisid: str = None, capture: list = None) -> GenerationResult:
    """Non-streaming generation with retry and multi-account failover.

    cookie_str/sapisid override the credentials for this call only (used by the
    console to validate a paste before saving); cookie_str="" forces anonymous.
    Without an override, accounts are tried in order; a credential-level failure
    first triggers one cookie rotation, then moves on to the next account.
    """
    result = _generate_detailed(prompt, model_id, think_mode, file_refs,
                                extra_fields, cookie_str, sapisid)
    if capture is not None:
        capture.append(result)
    return result


def _generate_detailed(prompt, model_id, think_mode, file_refs, extra_fields,
                       cookie_str, sapisid) -> GenerationResult:
    if cookie_str is not None:
        return _generate_once(None, prompt, model_id, think_mode, file_refs,
                              extra_fields, cookie_str, sapisid)
    order = _account_order()
    last_err = None
    for idx in order:
        renewed = False
        while True:
            try:
                return _generate_once(idx, prompt, model_id, think_mode, file_refs, extra_fields)
            except Exception as e:
                last_err = e
                if not renewed and _renew_account_cookies(idx, e):
                    renewed = True
                    continue
                if _is_account_error(e) and len(order) > 1:
                    log(f"Account {_acct_label(idx)} rejected the request "
                        f"(HTTP {_http_status(e) or type(e).__name__}); switching to the next account")
                    break
                raise
    raise last_err if last_err is not None else RuntimeError("no upstream account available")


def _generate_once(idx, prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                   extra_fields: dict = None, cookie_str: str = None,
                   sapisid: str = None) -> GenerationResult:
    """Try a single account. Account-level errors are raised immediately so the
    caller can renew or switch instead of burning retries on bad credentials."""
    auth_user, cookie, sap, xsrf = _account_ctx(idx)
    if cookie_str is not None:
        cookie, sap = cookie_str, (sapisid if cookie_str else None)
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields,
                          xsrf_token=xsrf).encode()
    url = _get_url(auth_user)
    headers = _build_headers(auth_user, cookie, sap)
    opener = _opener(CONFIG.get("proxy"))
    timeout = get_int("request_timeout_sec", 180)

    authenticated = bool(cookie)
    call_label = (_acct_label(idx) if cookie_str is None
                  else ("粘贴验证" if authenticated else "匿名验证"))
    n_cookie = len([p for p in (cookie or "").split(";") if "=" in p])
    match = re.match(r"^/u/(\d+)", urllib.parse.urlparse(url).path or "")
    where = f"/u/{match.group(1)}" if match else "无/u/N"
    t0 = time.time()
    log(f"→ {call_label} {where} model={model_id}({_MODE_NAMES.get(model_id, '?')}) "
        f"think={think_mode} prompt={len(prompt)}字 "
        f"cookie={'有(' + str(n_cookie) + '字段)' if n_cookie else '无(匿名)'} "
        f"xsrf={'有' if xsrf else '无'}")

    attempts = max(1, get_int("retry_attempts", 3))
    delay = max(0, CONFIG.get("retry_delay_sec", 2) or 0)
    last_err = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            response = opener.open(request, timeout=timeout)
            try:
                raw = response.read().decode("utf-8", errors="replace")
            finally:
                response.close()
            result = parse_result(raw)
            log(f"← {call_label} HTTP 200 {time.time()-t0:.1f}s 响应{len(result.text or '')}字"
                + (f" 思考{len(result.thoughts)}字" if result.thoughts else "")
                + (f" 图片{len(result.images)}张" if result.images else ""))
            return result
        except urllib.error.HTTPError as e:
            last_err = e
            log(f"← {call_label} HTTP {e.code} {time.time()-t0:.1f}s")
            if _is_account_error(e):
                raise      # credentials are the problem: let the caller renew/switch
            if e.code == 405 and update_bl_if_needed():
                url = _get_url(auth_user)
                log("Retrying with updated BL...")
                continue
            if attempt < attempts - 1:
                log(f"Retry {attempt+1}/{attempts}: {e}")
                time.sleep(delay)
        except BardError as e:
            last_err = e
            log(f"← {call_label} 上游 {e.code} {time.time()-t0:.1f}s {e}")
            if _is_account_error(e):
                raise
            if attempt < attempts - 1:
                log(f"Retry {attempt+1}/{attempts}: {e}")
                time.sleep(delay)
        except Exception as e:
            last_err = e
            if isinstance(e, ProtocolFrameError):
                log(f"← {call_label} 协议帧解码失败: {e}")
            if attempt < attempts - 1:
                log(f"Retry {attempt+1}/{attempts}: {e}")
                time.sleep(delay)
    if last_err is not None:
        status = _http_status(last_err)
        log(f"← {call_label} 失败 {('HTTP ' + str(status)) if status else type(last_err).__name__} "
            f"{time.time()-t0:.1f}s")
    raise last_err if last_err is not None else RuntimeError("generation failed with no error")


# ─── streaming ───────────────────────────────────────────────────────────────


class StreamState:
    """Tracks the incremental text/thoughts already handed to the client.

    Gemini resends the whole answer with every frame, so a delta is only the
    suffix that has not been emitted yet. Text that changes in the middle rather
    than growing means the upstream restarted the answer, which cannot be
    replayed into an SSE stream -- the caller is told instead of shipping a
    spliced transcript.
    """

    def __init__(self):
        self.primary_rcid = None
        self.text = ""
        self.thoughts = ""

    def deltas(self, candidates):
        """Yield ("text"|"thinking", delta) pairs for one frame's candidates."""
        for candidate in candidates:
            rcid = candidate.rcid or ""
            if self.primary_rcid is None:
                self.primary_rcid = rcid
            elif rcid and rcid != self.primary_rcid:
                continue        # ignore secondary candidates
            if candidate.text:
                for delta in self._advance("text", candidate.text):
                    yield ("text", delta)
            if candidate.thoughts:
                for delta in self._advance("thoughts", candidate.thoughts):
                    yield ("thinking", delta)

    def _advance(self, slot, value):
        previous = getattr(self, slot)
        if value == previous or previous.startswith(value):
            return []
        if not value.startswith(previous):
            raise RuntimeError("Gemini stream content changed during retry")
        delta = value[len(previous):]
        setattr(self, slot, value)
        if slot == "text":
            cleaned = clean_text(delta, strip=False)
            return [cleaned] if cleaned else []
        return [delta]


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                    extra_fields: dict = None):
    """Streaming generation yielding answer text deltas only."""
    for kind, delta in generate_stream_events(prompt, model_id, think_mode, file_refs, extra_fields):
        if kind == "text":
            yield delta


def generate_stream_events(prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                           extra_fields: dict = None):
    """Streaming generation yielding ("text"|"thinking", delta) tuples."""
    if not HAS_HTTPX:
        result = generate_detailed(prompt, model_id, think_mode, file_refs, extra_fields)
        if result.text:
            yield ("text", result.text)
        return

    order = _account_order()
    last_err = None
    for idx in order:
        streamed = False
        renewed = False
        while True:
            try:
                for event in _stream_once(idx, prompt, model_id, think_mode, file_refs, extra_fields):
                    streamed = True
                    yield event
                return
            except Exception as e:
                last_err = e
                if not streamed and not renewed and _renew_account_cookies(idx, e):
                    renewed = True
                    continue
                if streamed:
                    # Replaying on another account would duplicate output.
                    raise
                if _is_account_error(e) and len(order) > 1:
                    log(f"Account {_acct_label(idx)} rejected the stream "
                        f"(HTTP {_http_status(e) or type(e).__name__}); switching to the next account")
                    break
                raise
    raise last_err if last_err is not None else RuntimeError("no upstream account available")


def _stream_once(idx, prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                 extra_fields: dict = None):
    """Stream from one account. Account-level errors are raised immediately."""
    auth_user, cookie, sap, xsrf = _account_ctx(idx)
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, xsrf_token=xsrf)
    url = _get_url(auth_user)
    headers = _build_headers(auth_user, cookie, sap)
    client = _httpx_client(stream=True)
    if client is None:
        raise RuntimeError("httpx is required for streaming")

    attempts = max(1, get_int("retry_attempts", 3))
    delay = max(0, CONFIG.get("retry_delay_sec", 2) or 0)
    last_err = None
    for attempt in range(attempts):
        state = StreamState()
        try:
            with client.stream("POST", url, content=body, headers=headers) as response:
                response.raise_for_status()
                decoder = FrameDecoder()
                for chunk in response.iter_text():
                    for record in decoder.feed(chunk):
                        yield from _events_from_record(record, state)
                for record in decoder.close():
                    yield from _events_from_record(record, state)
            return
        except Exception as e:
            last_err = e
            if _is_account_error(e):
                raise      # credentials are the problem: let the caller renew/switch
            if _http_status(e) == 405 and update_bl_if_needed():
                log("BL updated, falling back to non-streaming for this request")
                result = generate_detailed(prompt, model_id, think_mode, file_refs, extra_fields)
                if result.text:
                    yield ("text", result.text)
                return
            if attempt < attempts - 1:
                log(f"Stream retry {attempt+1}/{attempts}: {e}")
                time.sleep(delay)
    raise last_err if last_err is not None else RuntimeError("stream failed with no error")


def _events_from_record(record, state):
    _raise_for_record_error(record)
    payload = record_payload(record)
    if payload is None:
        return
    yield from state.deltas(parse_payload(payload))
