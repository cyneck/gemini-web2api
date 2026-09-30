"""Multimodal support: Scotty resumable upload for Gemini image input."""
import re
import threading
import time
import urllib.request

from .config import CONFIG, get_int
from .gemini import _get_ssl_ctx, load_cookie, log, make_sapisidhash
from .netguard import ResponseTooLargeError, UnsafeURLError, fetch_bytes


def _get_page_tokens() -> dict:
    """Fetch WIZ_global_data tokens from the Gemini page (Push-ID, X-Client-Pctx)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    cookie_str, sapisid = load_cookie()
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(sapisid)
    try:
        request = urllib.request.Request("https://gemini.google.com/app", headers=headers)
        proxy = CONFIG.get("proxy")
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()),
            )
        else:
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=_get_ssl_ctx()))
        response = opener.open(request, timeout=30)
        try:
            html = response.read().decode("utf-8", errors="replace")
        finally:
            response.close()
        tokens = {}
        for key, pattern in [
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
            ("at", r'"thykhd":"([^"]+)"'),
        ]:
            match = re.search(pattern, html)
            if match:
                tokens[key] = match.group(1)
        return tokens
    except Exception as e:
        log(f"Page token fetch failed: {e}")
        return {}


_page_tokens_cache = {"tokens": {}, "ts": 0}
_page_tokens_lock = threading.Lock()
PAGE_TOKENS_TTL_SEC = 600


def _cached_page_tokens() -> dict:
    """Cached page tokens; refreshed at most every PAGE_TOKENS_TTL_SEC."""
    now = time.time()
    with _page_tokens_lock:
        stale = now - _page_tokens_cache["ts"] > PAGE_TOKENS_TTL_SEC
    if not stale:
        return _page_tokens_cache["tokens"]
    tokens = _get_page_tokens()
    with _page_tokens_lock:
        if tokens:
            _page_tokens_cache["tokens"] = tokens
        _page_tokens_cache["ts"] = now
        return _page_tokens_cache["tokens"]


def detect_image_mime(image_bytes: bytes, fallback: str = "image/png") -> str:
    """Infer a common raster image MIME type from its file signature."""
    if not isinstance(image_bytes, bytes):
        return fallback
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes.startswith(b"BM"):
        return "image/bmp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(image_bytes) >= 12 and image_bytes[4:8] == b"ftyp":
        brand = image_bytes[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx"):
            return "image/heic"
    return fallback


def upload_image(image_bytes: bytes, filename: str = "image.png",
                 mime_type: str = "image/png") -> str:
    """Upload image via Scotty resumable upload. Returns file reference path."""
    if not image_bytes:
        raise ValueError("cannot upload an empty image")
    tokens = _cached_page_tokens()
    push_id = tokens.get("push_id", "feeds/mcudyrk2a4khkz")
    pctx = tokens.get("pctx", "CgcSBWjK7pYx")

    cookie_str, sapisid = load_cookie()
    ctx = _get_ssl_ctx()
    proxy = CONFIG.get("proxy")

    # Step 1: initiate the resumable upload
    start_headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "X-Goog-Upload-Header-Content-Length": str(len(image_bytes)),
        "X-Goog-Upload-Header-Content-Type": mime_type,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if cookie_str:
        start_headers["Cookie"] = cookie_str
    if sapisid:
        start_headers["Authorization"] = make_sapisidhash(sapisid)

    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)

    start_url = "https://content-push.googleapis.com/upload/"
    request = urllib.request.Request(start_url, data=b"", headers=start_headers, method="POST")
    response = opener.open(request, timeout=30)
    try:
        upload_url = (response.headers.get("X-Goog-Upload-URL")
                      or response.headers.get("x-goog-upload-url"))
    finally:
        response.close()
    if not upload_url:
        raise RuntimeError("upstream did not return an upload URL")

    log(f"Upload session started: {upload_url[:80]}...")

    # Step 2: upload the bytes and finalize
    upload_headers = {
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/octet-stream",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    request = urllib.request.Request(upload_url, data=image_bytes,
                                    headers=upload_headers, method="POST")
    response = opener.open(request, timeout=60)
    try:
        file_ref = response.read().decode().strip()
    finally:
        response.close()
    if not file_ref or not file_ref.startswith("/"):
        raise RuntimeError("invalid file reference returned by upstream")

    log(f"Image uploaded: {filename} -> {file_ref[:50]}...")
    return file_ref


def fetch_image_bytes(url: str) -> bytes:
    """Fetch an image from a user-supplied URL.

    Remote URLs come straight from the API caller, so the request is validated
    against private/loopback/link-local ranges, capped in size, and limited in
    the number of redirects. An unsafe or oversized URL raises instead of
    returning an empty body, so the caller can answer with a real error instead
    of a confusing "image upload failed".
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("image URL is empty")
    body = fetch_bytes(
        url.strip(),
        max_bytes=get_int("max_media_fetch_bytes", 50 * 1024 * 1024),
        timeout=get_int("image_fetch_timeout_sec", 30),
        proxy=CONFIG.get("proxy"),
        ssl_ctx=_get_ssl_ctx(),
        allow_private=bool(CONFIG.get("image_fetch_allow_private_hosts")),
        log=log,
    )
    if not body:
        raise UnsafeURLError(f"remote image at {url} was empty")
    return body


__all__ = [
    "ResponseTooLargeError",
    "UnsafeURLError",
    "detect_image_mime",
    "fetch_image_bytes",
    "upload_image",
]
