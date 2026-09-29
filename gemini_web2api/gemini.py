"""Gemini StreamGenerate protocol implementation with httpx streaming."""
import json
import time
from collections import deque
import uuid
import re
import urllib.request
import urllib.error
import urllib.parse
import ssl
import os
import hashlib

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

from .config import CONFIG, resolve_path, get_accounts, account_cookie_path

_ssl_ctx = None
_cookie_cache = {}   # cookie file path -> {"str", "sapisid", "mtime"}
_httpx_client = None


LOG_BUFFER_SIZE = 500
_log_buffer = deque(maxlen=LOG_BUFFER_SIZE)
_log_seq = 0
_ERROR_HINTS = ("error", "failed", "retry", "barderrorinfo", "rejected",
                "traceback", "falling back", "timed out")


def log(msg: str):
    """Record a log line in the in-memory ring buffer (for /ui) and stderr."""
    global _log_seq
    _log_seq += 1
    lowered = msg.lower()
    level = "error" if any(h in lowered for h in _ERROR_HINTS) else "info"
    _log_buffer.append({"seq": _log_seq, "t": time.strftime('%H:%M:%S'),
                        "level": level, "msg": msg})
    if CONFIG["log_requests"]:
        import sys
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        sys.stderr.flush()


def get_logs(since: int = 0) -> list:
    """Return buffered log entries newer than `since`, plus the latest seq."""
    return [e for e in _log_buffer if e["seq"] > since]


def latest_log_seq() -> int:
    return _log_seq if _log_seq else (_log_buffer[-1]["seq"] if _log_buffer else 0)


def clear_logs():
    _log_buffer.clear()


def _get_ssl_ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def _get_httpx_client():
    global _httpx_client
    if _httpx_client is None and HAS_HTTPX:
        proxy = CONFIG.get("proxy")
        transport = httpx.HTTPTransport(proxy=proxy) if proxy else None
        _httpx_client = httpx.Client(transport=transport, timeout=CONFIG["request_timeout_sec"], verify=True)
    return _httpx_client


def load_cookie(auth_user=None) -> tuple:
    """Load one account's cookie from file, with mtime-based caching.

    auth_user=None means the active account. Each account keeps its own cache
    entry keyed by file path, so switching accounts never serves stale data.
    """
    cookie_file = resolve_path(account_cookie_path(auth_user))
    if not cookie_file or not os.path.exists(cookie_file):
        return "", None
    try:
        mtime = os.path.getmtime(cookie_file)
        cached = _cookie_cache.get(cookie_file)
        if cached and cached["mtime"] == mtime and cached["str"]:
            return cached["str"], cached["sapisid"]
        with open(cookie_file, "r") as f:
            content = f.read().strip()
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
        _cookie_cache[cookie_file] = {"str": cookie_str, "sapisid": sapisid or None, "mtime": mtime}
        return cookie_str, sapisid if sapisid else None
    except Exception as e:
        log(f"Cookie load error: {e}")
        cached = _cookie_cache.get(cookie_file) or {}
        return cached.get("str", ""), cached.get("sapisid")


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


def _apply_chat_persistence_flags(inner: list) -> None:
    """Apply Gemini Web persistence flags to an outgoing request payload."""
    if CONFIG.get("temporary_chats", False):
        # Match Gemini Web temporary-chat requests.
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def _build_payload(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None,
                   xsrf_token: str = None) -> str:
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
        req = urllib.request.Request(
            "https://gemini.google.com/app",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        ctx = _get_ssl_ctx()
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=ctx))
            resp = opener.open(req, timeout=15)
        else:
            resp = urllib.request.urlopen(req, context=ctx, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
        m = re.search(r'(boq_assistant-bard-web-server_\d+\.\d+_p\d+)', html)
        if m:
            return m.group(1)
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


def clean_text(text: str, strip: bool = True) -> str:
    text = re.sub(
        r'```(?:python|javascript|text)\?code_(?:reference|stdout)&code_event_index=\d+\n.*?```\n?',
        '', text, flags=re.DOTALL
    )
    text = re.sub(r'http://googleusercontent\.com/card_content/\d+\n?', '', text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list:
    """Parse a single wrb.fr line and return list of text strings found."""
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and part[1] and isinstance(part[1], list):
                for t in part[1]:
                    if isinstance(t, str) and t:
                        texts.append(t)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Parse full response to get final text."""
    bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', raw)
    if bard_err:
        raise RuntimeError(f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]")
    last_text = ""
    for line in raw.split("\n"):
        for t in _extract_texts_from_line(line):
            if len(t) > len(last_text):
                last_text = t
    return clean_text(last_text)


# Status codes that mean "this account cannot serve the request" and justify
# moving to the next one: bad/expired credentials, missing permission, quota.
_ACCOUNT_FAIL_CODES = (400, 401, 403, 429)

# MODE_CATEGORY enum from the Gemini frontend JS. Several model names share one
# pair of ids, so logging the category says more than logging the raw number.
_MODE_NAMES = {1: "FAST", 2: "THINKING", 3: "PRO", 4: "AUTO",
               5: "FAST_DYN", 6: "FLASH_LITE"}


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
    a = get_accounts()[idx]
    auth_user = a.get("auth_user")
    cookie_str, sapisid = load_cookie(auth_user)
    return auth_user, cookie_str, sapisid, a.get("xsrf_token")


def _acct_label(idx) -> str:
    if idx is None:
        return "default (no account configured)"
    a = get_accounts()[idx]
    au = a.get("auth_user")
    return f"/u/{au}" if au is not None and au != "" else f"account#{idx}"


def _http_status(e) -> int:
    """HTTP status carried by an exception (urllib or httpx), else 0."""
    code = getattr(e, "code", 0)
    if code:
        return code
    return getattr(getattr(e, "response", None), "status_code", 0) or 0


def _is_account_error(e) -> bool:
    """True when the failure is about the account, not the network.

    ValueError covers a malformed credential (e.g. a cookie containing a
    newline), which urllib rejects before the request is ever sent.
    """
    return _http_status(e) in _ACCOUNT_FAIL_CODES or isinstance(e, ValueError)


def generate(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None,
             cookie_str: str = None, sapisid: str = None) -> str:
    """Non-streaming generation with retry and multi-account failover.

    cookie_str/sapisid override the credentials for this call only (used by the
    console to validate a paste before saving); cookie_str="" forces anonymous.
    Without an override, accounts are tried in order and a credential-level
    failure (400/401/403/429) moves on to the next configured account.
    """
    if cookie_str is not None:
        return _generate_once(None, prompt, model_id, think_mode, file_refs, extra_fields,
                              cookie_str, sapisid)
    order = _account_order()
    last_err = None
    for idx in order:
        try:
            return _generate_once(idx, prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as e:
            last_err = e
            if _is_account_error(e) and len(order) > 1:
                log(f"Account {_acct_label(idx)} rejected the request (HTTP {e.code}); switching to the next account")
                continue
            raise
    raise last_err


def _generate_once(idx, prompt: str, model_id: int, think_mode: int, file_refs: list = None,
                   extra_fields: dict = None, cookie_str: str = None, sapisid: str = None) -> str:
    """Try a single account. Account-level errors are raised immediately so the
    caller can switch accounts instead of burning retries on bad credentials."""
    auth_user, ck, sap, xsrf = _account_ctx(idx)
    if cookie_str is not None:
        ck, sap = cookie_str, (sapisid if cookie_str else None)
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, xsrf_token=xsrf).encode()
    url = _get_url(auth_user)
    headers = _build_headers(auth_user, ck, sap)
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    # One line per request with everything needed to tell "which account, which
    # model, which credentials" apart. Never log the cookie value itself.
    n_cookie = len([p for p in (ck or "").split(";") if "=" in p])
    call_label = (_acct_label(idx) if cookie_str is None
                  else ("粘贴验证" if ck else "匿名验证"))
    m = re.match(r"^/u/(\d+)", urllib.parse.urlparse(url).path or "")
    where = f"/u/{m.group(1)}" if m else "无/u/N"
    t0 = time.time()
    log(f"→ {call_label} {where} model={model_id}({_MODE_NAMES.get(model_id, '?')}) "
        f"think={think_mode} prompt={len(prompt)}字 "
        f"cookie={'有(' + str(n_cookie) + '字段)' if n_cookie else '无(匿名)'} "
        f"xsrf={'有' if xsrf else '无'}")

    last_err = None
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            if proxy:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                    urllib.request.HTTPSHandler(context=ctx)
                )
                resp = opener.open(req, timeout=CONFIG["request_timeout_sec"])
            else:
                resp = urllib.request.urlopen(req, context=ctx, timeout=CONFIG["request_timeout_sec"])
            raw = resp.read().decode("utf-8", errors="replace")
            text = extract_response_text(raw)
            log(f"← {call_label} HTTP 200 {time.time()-t0:.1f}s 响应{len(text or '')}字")
            return text
        except urllib.error.HTTPError as e:
            last_err = e
            log(f"← {call_label} HTTP {e.code} {time.time()-t0:.1f}s")
            if _is_account_error(e):
                raise      # credentials are the problem: let the caller switch accounts
            if e.code == 405 and update_bl_if_needed():
                url = _get_url(auth_user)
                log("Retrying with updated BL...")
                continue
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
        except Exception as e:
            last_err = e
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    if last_err is not None:
        status = _http_status(last_err)
        log(f"← {call_label} 失败 {('HTTP ' + str(status)) if status else type(last_err).__name__} "
            f"{time.time()-t0:.1f}s")
    raise last_err


def generate_stream(prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Streaming generation via httpx, with multi-account failover.

    A credential-level rejection before any output switches to the next
    account. Once bytes have been streamed the failure is raised as-is,
    because replaying on another account would duplicate the response.
    """
    if not HAS_HTTPX:
        text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    order = _account_order()
    last_err = None
    for idx in order:
        streamed = False
        try:
            for chunk in _stream_once(idx, prompt, model_id, think_mode, file_refs, extra_fields):
                streamed = True
                yield chunk
            return
        except Exception as e:
            last_err = e
            if streamed:
                raise
            if _is_account_error(e) and len(order) > 1:
                log(f"Account {_acct_label(idx)} rejected the stream (HTTP {_http_status(e)}); switching to the next account")
                continue
            raise
    raise last_err


def _stream_once(idx, prompt: str, model_id: int, think_mode: int, file_refs: list = None, extra_fields: dict = None):
    """Stream from one account. Account-level errors are raised immediately."""
    auth_user, ck, sap, xsrf = _account_ctx(idx)
    body = _build_payload(prompt, model_id, think_mode, file_refs, extra_fields, xsrf_token=xsrf)
    url = _get_url(auth_user)
    headers = _build_headers(auth_user, ck, sap)
    client = _get_httpx_client()

    last_err = None
    emitted_raw_text = ""
    for attempt in range(CONFIG["retry_attempts"]):
        try:
            with client.stream("POST", url, content=body, headers=headers) as resp:
                resp.raise_for_status()
                buf = ""
                for chunk in resp.iter_text():
                    buf += chunk
                    if "BardErrorInfo" in buf:
                        bard_err = re.search(r'BardErrorInfo\s*\[(\d+)\]', buf)
                        if bard_err:
                            raise RuntimeError(
                                f"Gemini upstream rejected request: BardErrorInfo [{bard_err.group(1)}]"
                            )
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        for t in _extract_texts_from_line(line):
                            if t == emitted_raw_text or emitted_raw_text.startswith(t):
                                continue
                            if not t.startswith(emitted_raw_text):
                                raise RuntimeError("Gemini stream content changed during retry")
                            delta = clean_text(t[len(emitted_raw_text):], strip=False)
                            emitted_raw_text = t
                            if delta:
                                yield delta
            return
        except Exception as e:
            last_err = e
            if _is_account_error(e):
                raise      # credentials are the problem: let the caller switch accounts
            if _http_status(e) == 405 and update_bl_if_needed():
                log("BL updated, falling back to non-streaming for this request")
                text = generate(prompt, model_id, think_mode, file_refs, extra_fields)
                if text:
                    yield text
                return
            if attempt < CONFIG["retry_attempts"] - 1:
                log(f"Stream retry {attempt+1}/{CONFIG['retry_attempts']}: {e}")
                time.sleep(CONFIG["retry_delay_sec"])
    raise last_err
