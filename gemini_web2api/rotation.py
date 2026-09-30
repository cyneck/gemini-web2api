"""Portable `__Secure-1PSIDTS` rotation (cookie auto-renewal).

Google expires `__Secure-1PSIDTS` on a short schedule. Without renewal the
service starts answering HTTP 400/403 and every account has to be re-exported by
hand. `accounts.google.com/RotateCookies` hands out a fresh value from plain
cookies plus SAPISID -- no browser profile, no device-bound key material -- so it
works on Linux, macOS and inside containers, which is exactly where the console's
manual export flow is unavailable.

The endpoint rate-limits aggressively (HTTP 429), hence the per-cookie throttle.
"""
import hashlib
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROTATE_URL = "https://accounts.google.com/RotateCookies"
# Exact body the web client sends; the numbers are opaque protocol markers.
ROTATE_BODY = '[000,"-0000000000000000000"]'
MIN_INTERVAL_SEC = 60
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_lock = threading.Lock()
_last_attempt = {}


class RotationSkipped(RuntimeError):
    """Rotation did not run (throttled, or no rotatable session)."""


def parse_cookies(cookie_str):
    """Parse a `name=value; name=value` string into an ordered dict."""
    pairs = {}
    for chunk in (cookie_str or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if name:
            pairs[name] = value
    return pairs


def format_cookies(pairs):
    return "; ".join(f"{name}={value}" for name, value in pairs.items())


def merge_set_cookie(cookie_str, set_cookie_headers):
    """Merge HTTP Set-Cookie values into a cookie header string."""
    pairs = parse_cookies(cookie_str)
    changed = False
    for header in set_cookie_headers or []:
        if not header:
            continue
        first = header.split(";", 1)[0].strip()
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        if pairs.get(name) != value:
            pairs[name] = value
            changed = True
    if not changed:
        return cookie_str
    return format_cookies(pairs)


def has_rotatable_session(cookie_str):
    """The rotate endpoint needs both a session cookie and SAPISID."""
    pairs = parse_cookies(cookie_str)
    return bool(pairs.get("__Secure-1PSID")) and bool(pairs.get("SAPISID"))


def _throttle_key(cookie_str):
    pairs = parse_cookies(cookie_str)
    seed = pairs.get("__Secure-1PSID") or cookie_str or ""
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:16]


def _may_attempt(key, min_interval):
    now = time.time()
    with _lock:
        last = _last_attempt.get(key, 0)
        if now - last < min_interval:
            return False
        _last_attempt[key] = now
        if len(_last_attempt) > 512:
            cutoff = now - max(min_interval, 60) * 10
            for stale in [k for k, v in _last_attempt.items() if v < cutoff]:
                _last_attempt.pop(stale, None)
        return True


def reset_throttle():
    """Test helper: forget every recorded rotation attempt."""
    with _lock:
        _last_attempt.clear()


def rotate_cookies(cookie_str, *, proxy=None, timeout=20, ssl_ctx=None,
                   min_interval=MIN_INTERVAL_SEC, log=None, opener=None):
    """Rotate `__Secure-1PSIDTS` once.

    Returns the updated cookie string, or None when rotation was throttled or
    when the account has no session cookies to rotate. Raises on transport
    errors and on HTTP failures so the caller can decide to give up.
    """
    if not has_rotatable_session(cookie_str):
        raise RotationSkipped("account has no __Secure-1PSID/SAPISID to rotate")

    key = _throttle_key(cookie_str)
    if not _may_attempt(key, min_interval):
        raise RotationSkipped("rotation throttled")

    request = urllib.request.Request(
        ROTATE_URL,
        data=ROTATE_BODY.encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": "https://accounts.google.com",
            "Accept": "*/*",
            "Cookie": cookie_str,
            "User-Agent": USER_AGENT,
        },
    )
    if opener is None:
        handlers = [urllib.request.HTTPSHandler(context=ssl_ctx or ssl.create_default_context())]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)
    response = opener.open(request, timeout=timeout)
    try:
        headers = response.headers.get_all("Set-Cookie") or []
        updated = merge_set_cookie(cookie_str, headers)
        status = getattr(response, "status", 200) or 200
    finally:
        response.close()

    if not has_rotatable_session(updated):
        raise RuntimeError("rotation completed but left no usable session cookie")
    if log:
        before = parse_cookies(cookie_str).get("__Secure-1PSIDTS")
        after = parse_cookies(updated).get("__Secure-1PSIDTS")
        if before != after:
            log(f"Cookie rotation HTTP {status}: __Secure-1PSIDTS renewed")
        else:
            log(f"Cookie rotation HTTP {status}: no new __Secure-1PSIDTS")
    return updated


def persist_cookies(path, cookie_str):
    """Write a rotated cookie string back, preserving the file's format."""
    if not path:
        return False
    existing = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
        except OSError:
            existing = ""
    pairs = parse_cookies(cookie_str)

    if existing.strip().startswith("{"):
        try:
            payload = json.loads(existing)
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["cookie"] = cookie_str
        sapisid = pairs.get("SAPISID")
        if sapisid:
            payload["sapisid"] = sapisid
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        content = cookie_str + "\n"

    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    handle_fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return True


def renew_cookie_file(path, *, proxy=None, timeout=20, ssl_ctx=None,
                      min_interval=MIN_INTERVAL_SEC, log=None):
    """Rotate and persist the cookies stored at `path`.

    Returns True when new cookies were written. Never raises for the common
    "cannot renew" cases -- renewal is best effort, and a hard failure here must
    not break the request that triggered it.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError as error:
        if log:
            log(f"Cookie rotation skipped: cannot read {path}: {error}")
        return False

    cookie_str = raw
    if raw.startswith("{"):
        try:
            cookie_str = json.loads(raw).get("cookie", "") or ""
        except ValueError:
            cookie_str = ""
    if not cookie_str:
        return False

    try:
        updated = rotate_cookies(cookie_str, proxy=proxy, timeout=timeout,
                                 ssl_ctx=ssl_ctx, min_interval=min_interval, log=log)
    except RotationSkipped as error:
        if log:
            log(f"Cookie rotation skipped: {error}")
        return False
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, RuntimeError) as error:
        if log:
            log(f"Cookie rotation failed: {type(error).__name__}: {error}")
        return False
    if not updated or updated == cookie_str:
        return False
    try:
        persist_cookies(path, updated)
    except OSError as error:
        if log:
            log(f"Cookie rotation could not be saved to {path}: {error}")
        return False
    return True


__all__ = [
    "MIN_INTERVAL_SEC",
    "ROTATE_BODY",
    "ROTATE_URL",
    "RotationSkipped",
    "format_cookies",
    "has_rotatable_session",
    "merge_set_cookie",
    "parse_cookies",
    "persist_cookies",
    "renew_cookie_file",
    "reset_throttle",
    "rotate_cookies",
]
