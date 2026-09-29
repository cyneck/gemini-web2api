"""Built-in web console (H5 UI) + management API.

Serves a single-page Chinese console at /ui and JSON management endpoints
under /api/*. Write endpoints reuse the same api_keys auth gate as /v1/*.
Cookie values are never echoed back in full; only masked hints are shown.
"""
import json
import os
import re
import time
import urllib.error

from .config import CONFIG, save_config, config_path, resolve_path, get_accounts, get_account
from .models import MODELS, resolve_model
from .gemini import generate, load_cookie, HAS_HTTPX
from . import __version__

# Every cookie found in the paste is forwarded, so these lists are only a
# health check, never a filter. Keep them minimal: rejecting a paste that
# would actually work is worse than accepting one that is thin.
REQUIRED_COOKIES = ["SAPISID"]
# Any one of these proves a signed-in session.
SESSION_COOKIES = ["__Secure-1PSID", "__Secure-3PSID", "SID"]
# Shown in the console as "what we recognised" — the classic six, for reference.
DISPLAY_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID"]
TEST_PROMPT = "Reply with the single word: OK"


def missing_credentials(pairs: dict) -> list:
    """Credentials without which a request cannot possibly succeed."""
    missing = []
    if not pairs.get("SAPISID"):
        missing.append("SAPISID")
    if not any(pairs.get(k) for k in SESSION_COOKIES):
        missing.append("任一会话 cookie（" + " / ".join(SESSION_COOKIES) + "）")
    return missing


# ─── cookie parsing ──────────────────────────────────────────────────────────

# Gemini's XSRF token, embedded in the app HTML as "SNlM0e":"<value>".
# Required for authenticated requests; anonymous ones do not need it.
_XSRF_RE = re.compile(r'SNlM0e["\']?\s*[:=]\s*["\']([^"\']+)["\']')


def extract_xsrf_token(text: str) -> str:
    """Pull SNlM0e out of page source, a fetch snippet, or a bare token."""
    if not text:
        return ""
    m = _XSRF_RE.search(text)
    return m.group(1) if m else ""


def parse_cookie_input(text: str) -> dict:
    """Extract the Gemini cookie string from arbitrary user paste.

    Accepts: raw "a=b; c=d" strings, multi-line request headers, DevTools
    "Copy as fetch" code blocks, JSON {"cookie": ..., "sapisid": ...}.
    Returns {"cookie_str", "sapisid", "found", "missing", "xsrf"}.
    """
    text = (text or "").strip()
    xsrf = ""
    sapisid_hint = ""
    if not text:
        return {"cookie_str": "", "sapisid": "", "found": [],
                "missing": missing_credentials({}), "xsrf": ""}
    # Keep the original: the cookie regex below may rewrite `text` when page
    # source is pasted, which would otherwise hide the SNlM0e token.
    raw = text

    if text.startswith("{"):
        try:
            data = json.loads(text)
            text = str(data.get("cookie", ""))
            sapisid_hint = str(data.get("sapisid", "") or "")
        except (json.JSONDecodeError, ValueError):
            pass

    # "cookie": "..." (fetch code) or cookie: ... (request header)
    m = re.search(r'["\']?cookie["\']?\s*:\s*["\']([^"\']+)["\']', text, re.IGNORECASE)
    if m:
        text = m.group(1)
    else:
        m = re.search(r'(?:^|\n)\s*cookie\s*:\s*(.+)', text, re.IGNORECASE)
        if m:
            text = m.group(1).strip().strip('"').strip("'")

    # Newlines are separators too: DevTools and browser exports wrap long
    # cookie dumps across lines, and a stray \n makes the Cookie header invalid
    # (urllib raises "Invalid header value" instead of sending the request).
    text = re.sub(r"[\r\n]+", ";", text)
    pairs = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()

    found = [k for k in DISPLAY_COOKIES if pairs.get(k)]
    missing = missing_credentials(pairs)
    cookie_str = "; ".join(f"{k}={pairs[k]}" for k in pairs)
    sapisid = pairs.get("SAPISID") or sapisid_hint or None

    # opportunistically pull an XSRF token if the page source was pasted
    xsrf = extract_xsrf_token(raw) or extract_xsrf_token(text)

    return {"cookie_str": cookie_str, "sapisid": sapisid, "found": found, "missing": missing, "xsrf": xsrf}


def _mask_hint(cookie_str: str) -> str:
    pairs = [p.strip() for p in cookie_str.split(";") if "=" in p]
    hints = []
    for p in pairs:
        k, v = p.split("=", 1)
        if k in DISPLAY_COOKIES and v:
            hints.append(f"{k}={v[:4]}…{v[-4:]}" if len(v) > 8 else f"{k}={v[:2]}…")
    return "; ".join(hints)


def _mask_token(value) -> str:
    """Short masked preview of a token, so the console can show what it stored."""
    v = str(value or "").strip()
    if not v:
        return ""
    if len(v) <= 12:
        return v[:4] + "…"
    return f"{v[:6]}…{v[-4:]}"


def _cookie_file_path() -> str:
    return resolve_path(CONFIG.get("cookie_file") or "cookie.txt")


def _cookie_state() -> dict:
    cookie_str, sapisid = load_cookie()
    state = {
        "configured": bool(cookie_str),
        "sapisid": bool(sapisid),
        "path": os.path.abspath(_cookie_file_path()),
        "hint": _mask_hint(cookie_str) if cookie_str else "",
        "found": [], "missing": missing_credentials({}),
    }
    if cookie_str:
        pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
        state["found"] = [k for k in DISPLAY_COOKIES if pairs.get(k)]
        state["missing"] = missing_credentials(pairs)
    return state


# ─── management API ──────────────────────────────────────────────────────────

def handle_api(handler, method: str, path: str) -> bool:
    """Dispatch /api/* requests. Returns True if the request was handled."""
    if not path.startswith("/api/"):
        return False
    routes = {
        ("GET", "/api/status"): _api_status,
        ("GET", "/api/logs"): _api_logs,
        ("POST", "/api/config"): _api_config,
        ("POST", "/api/cookie/validate"): _api_cookie_validate,
        ("POST", "/api/cookie"): _api_cookie_save,
        ("POST", "/api/test"): _api_test,
        ("GET", "/api/accounts"): _api_accounts_list,
        ("POST", "/api/accounts"): _api_accounts_save,
        ("POST", "/api/accounts/activate"): _api_accounts_activate,
    }
    if method == "DELETE" and path == "/api/cookie":
        _api_cookie_clear(handler)
        return True
    if method == "DELETE" and path == "/api/accounts":
        _api_accounts_delete(handler)
        return True
    if method == "DELETE" and path == "/api/logs":
        _api_logs_clear(handler)
        return True
    fn = routes.get((method, path))
    if fn is None:
        handler.send_json({"error": "not found"}, 404)
        return True
    fn(handler)
    return True


def _read_json_body(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", 0))
        body = handler.rfile.read(length) if length else b""
        return json.loads(body or b"{}")
    except (ValueError, json.JSONDecodeError):
        return {}


def _api_status(handler):
    cfg_keys = CONFIG.get("api_keys") or []
    handler.send_json({
        "version": __version__,
        "host": CONFIG["host"], "port": CONFIG["port"],
        "base_url": f"http://localhost:{CONFIG['port']}/v1",
        "proxy": CONFIG.get("proxy"),
        "default_model": CONFIG.get("default_model"),
        "models": {k: v["desc"] for k, v in MODELS.items()},
        "auth_enabled": bool(cfg_keys),
        "api_keys_count": len(cfg_keys),
        "auth_user": CONFIG.get("auth_user"),
        "xsrf_token": (CONFIG.get("xsrf_token") or "")[:8] + "…" if CONFIG.get("xsrf_token") else None,
        "gemini_bl": CONFIG.get("gemini_bl"),
        "log_requests": bool(CONFIG.get("log_requests")),
        "streaming": HAS_HTTPX,
        "config_path": config_path(),
        "cookie": _cookie_state(),
    })


_CONFIG_FIELDS = ["proxy", "default_model", "auth_user", "xsrf_token",
                  "gemini_bl", "log_requests", "request_timeout_sec",
                  "retry_attempts", "retry_delay_sec"]


def _api_config(handler):
    req = _read_json_body(handler)
    if not req:
        handler.send_json({"error": "invalid JSON body"}, 400)
        return
    if "api_keys" in req:
        keys = req["api_keys"]
        if keys is None:
            CONFIG["api_keys"] = []
        elif isinstance(keys, list):
            CONFIG["api_keys"] = [str(k).strip() for k in keys if str(k).strip()]
        else:
            handler.send_json({"error": "api_keys must be a list"}, 400)
            return
    if "xsrf_token" in req:
        raw = req["xsrf_token"]
        if raw in (None, ""):
            CONFIG["xsrf_token"] = None
        else:
            s = str(raw).strip()
            # A bare token is short; anything long or containing the key is
            # page source, so pull the token out of it.
            if len(s) > 200 or "SNlM0e" in s:
                tok = extract_xsrf_token(s)
                if not tok:
                    handler.send_json({"error": "未能从粘贴内容中识别 SNlM0e，请确认粘贴的是 gemini.google.com 的网页源代码（Ctrl+U 全选复制）"}, 400)
                    return
                s = tok
            CONFIG["xsrf_token"] = s or None
    for f in _CONFIG_FIELDS:
        if f in req and f != "xsrf_token":
            val = req[f]
            if f in ("auth_user", "xsrf_token", "proxy", "default_model", "gemini_bl"):
                CONFIG[f] = str(val).strip() if val not in (None, "") else None
            elif f == "log_requests":
                CONFIG[f] = bool(val)
            else:
                try:
                    CONFIG[f] = int(val)
                except (TypeError, ValueError):
                    handler.send_json({"error": f"{f} must be a number"}, 400)
                    return
    try:
        saved = save_config()
    except RuntimeError as e:
        handler.send_json({"error": str(e)}, 400)
        return
    handler.send_json({"ok": True, "saved": saved,
                       "auth_enabled": bool(CONFIG.get("api_keys")),
                       "api_keys_count": len(CONFIG.get("api_keys") or [])})


def _try_generate(cookie_str=None, sapisid=None, model="gemini-3.5-flash"):
    """Returns (ok, detail, text). Distinguishes auth failure from network failure."""
    _, mode_id, think_mode, err, extra = resolve_model(model)
    if err:
        return False, err, ""
    t0 = time.time()
    try:
        text = generate(TEST_PROMPT, mode_id, think_mode, extra_fields=extra,
                        cookie_str=cookie_str, sapisid=sapisid)
    except urllib.error.HTTPError as e:
        code = e.code
        if code == 400 and cookie_str:
            return False, ("Gemini 返回 400：该账号的 cookie 可能被拒绝（失效/导出不完整/触发风控）。"
                           "请重新导出 cookie，或补一个 xsrf_token（SNlM0e）"), ""
        if code in (400, 401, 403):
            return False, f"Gemini 拒绝了请求 (HTTP {code})，cookie 可能已过期或缺少权限", ""
        return False, f"Gemini 上游返回 HTTP {code}", ""
    except Exception as e:
        return False, f"网络/代理问题：{type(e).__name__}: {e}", ""
    latency = int((time.time() - t0) * 1000)
    if not text or not text.strip():
        return False, "Gemini 返回了空内容（可能被限流或触发风控）", ""
    return True, f"验证成功，延迟 {latency} ms", text.strip()


def _api_logs(handler):
    import urllib.parse
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
    try:
        since = int(qs.get("since", ["0"])[0])
    except (TypeError, ValueError, IndexError):
        since = 0
    from .gemini import get_logs, latest_log_seq
    handler.send_json({"logs": get_logs(since), "seq": latest_log_seq()})


def _api_logs_clear(handler):
    from .gemini import clear_logs
    clear_logs()
    handler.send_json({"ok": True})


def _api_cookie_validate(handler):
    req = _read_json_body(handler)
    parsed = parse_cookie_input(str(req.get("cookie", "")))
    if not parsed["cookie_str"]:
        handler.send_json({"valid": False, "found": [], "missing": missing_credentials({}),
                           "detail": "没有从输入中解析到任何 cookie"}, 200)
        return
    if parsed["missing"]:
        handler.send_json({"valid": False, "found": parsed["found"], "missing": parsed["missing"],
                           "xsrf": parsed["xsrf"],
                           "detail": "缺少关键 cookie：" + ", ".join(parsed["missing"])}, 200)
        return
    ok, detail, _ = _try_generate(cookie_str=parsed["cookie_str"], sapisid=parsed["sapisid"])
    handler.send_json({"valid": ok, "found": parsed["found"], "missing": [],
                       "xsrf": parsed["xsrf"], "detail": detail}, 200)


def _api_cookie_save(handler):
    req = _read_json_body(handler)
    parsed = parse_cookie_input(str(req.get("cookie", "")))
    if not parsed["cookie_str"]:
        # Page source carries SNlM0e but no cookie header: still allow
        # refreshing just the XSRF token, otherwise there is no way to fix a
        # "configured cookie + missing xsrf" 400 without re-pasting cookies.
        if parsed["xsrf"]:
            CONFIG["xsrf_token"] = parsed["xsrf"]
            try:
                save_config()
            except RuntimeError:
                pass
            handler.send_json({"ok": True, "saved": None, "cookie_saved": False,
                               "found": [], "hint": "", "xsrf": parsed["xsrf"],
                               "xsrf_saved": True})
            return
        handler.send_json({"error": "没有解析到任何 cookie，也没找到 SNlM0e"}, 400)
        return
    if parsed["missing"]:
        handler.send_json({"error": "缺少关键 cookie：" + ", ".join(parsed["missing"]),
                           "missing": parsed["missing"]}, 400)
        return
    path = _cookie_file_path()
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(parsed["cookie_str"] + "\n")
    # A cookie without SNlM0e makes Gemini reject every request with 400, so
    # persist it right here instead of making the user save config separately.
    xsrf_saved = False
    need_save = False
    if parsed["xsrf"]:
        CONFIG["xsrf_token"] = parsed["xsrf"]
        xsrf_saved = True
        need_save = True
    if not CONFIG.get("cookie_file"):
        CONFIG["cookie_file"] = path
        need_save = True
    if need_save:
        try:
            save_config()
        except RuntimeError:
            pass
    handler.send_json({"ok": True, "saved": os.path.abspath(path),
                       "found": parsed["found"], "hint": _mask_hint(parsed["cookie_str"]),
                       "xsrf": parsed["xsrf"], "xsrf_saved": xsrf_saved})


def _api_cookie_clear(handler):
    """Remove the cookie file but keep the configured cookie_file path intact."""
    path = _cookie_file_path()
    removed = False
    if os.path.exists(path):
        os.remove(path)
        removed = True
    handler.send_json({"ok": True, "removed": removed, "anonymous": True})


# ─── accounts (one Google account == one auth_user + cookie + xsrf) ──────────

def _default_cookie_name(auth_user) -> str:
    suffix = "default" if auth_user in (None, "") else str(auth_user)
    return f"cookie_u{suffix}.txt"


def _read_cookie_at(path) -> str:
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _api_accounts_list(handler):
    accts = get_accounts()
    active = CONFIG.get("active_account") or 0
    out = []
    for i, a in enumerate(accts):
        cookie_str = _read_cookie_at(resolve_path(a.get("cookie_file")))
        pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p) if cookie_str else {}
        found = [k for k in DISPLAY_COOKIES if pairs.get(k)]
        missing = missing_credentials(pairs)
        out.append({
            "idx": i,
            "auth_user": a.get("auth_user"),
            "label": a.get("label") or "",
            "enabled": a.get("enabled", True),
            "active": i == active,
            "cookie_file": a.get("cookie_file"),
            "cookie_configured": bool(cookie_str),
            "found": found,
            "missing": missing,
            "hint": _mask_hint(cookie_str) if cookie_str else "",
            "xsrf_set": bool(a.get("xsrf_token")),
            "xsrf_hint": _mask_token(a.get("xsrf_token")),
            # xsrf_token is optional; a malformed/invalid cookie is the usual
            # cause of a 400, and failover handles it at request time.
            "ready": bool(cookie_str) and not missing,
        })
    handler.send_json({"accounts": out, "active_account": active if accts else None,
                       "anonymous": not accts})


def _api_accounts_save(handler):
    """Create or update one account.

    Parses the cookie and the xsrf_token independently: the paste may carry
    either, or both (page source usually contains SNlM0e).
    """
    req = _read_json_body(handler) or {}
    cookie_paste = str(req.get("cookie") or "")
    parsed = parse_cookie_input(cookie_paste)

    xsrf_in = str(req.get("xsrf") or "").strip()
    if len(xsrf_in) > 200 or "SNlM0e" in xsrf_in:
        xsrf_in = extract_xsrf_token(xsrf_in) or xsrf_in
    xsrf = xsrf_in or parsed["xsrf"] or None

    # Only overwrite a field when the caller actually sent it: the two parse
    # buttons each submit just their own field, and an absent key must never
    # wipe a value that is already stored on the account.
    has_auth_user = "auth_user" in req
    auth_user = None
    if has_auth_user:
        raw_user = req.get("auth_user")
        if raw_user not in (None, ""):
            # /u/N is Google's numeric account index; anything else produces a
            # bogus URL and Gemini answers 404. Use "label" for human names.
            try:
                auth_user = int(str(raw_user).strip())
            except (TypeError, ValueError):
                handler.send_json({"error": "auth_user 必须是数字序号（0、1、2…，对应 Google 的 /u/N）；"
                                           "想给账号起名字请填「备注名」"}, 400)
                return

    accts = get_accounts()
    idx = req.get("idx")
    if idx is None:
        acct = {"auth_user": auth_user if has_auth_user else None, "label": "",
                "cookie_file": None, "xsrf_token": None, "enabled": True}
        accts.append(acct)
        idx = len(accts) - 1
    else:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            handler.send_json({"error": "idx 必须是数字"}, 400)
            return
        if not (0 <= idx < len(accts)):
            handler.send_json({"error": f"账号不存在：{idx}"}, 400)
            return
        acct = accts[idx]
        if has_auth_user:
            acct["auth_user"] = auth_user
    if "label" in req:
        acct["label"] = str(req.get("label") or "").strip()
    if "enabled" in req:
        acct["enabled"] = bool(req.get("enabled"))
    if xsrf:
        acct["xsrf_token"] = xsrf

    cookie_saved = False
    if parsed["cookie_str"]:
        if parsed["missing"]:
            handler.send_json({"error": "缺少关键 cookie：" + ", ".join(parsed["missing"]),
                               "missing": parsed["missing"]}, 400)
            return
        eff_user = auth_user if auth_user is not None else acct.get("auth_user")
        path = (resolve_path(acct.get("cookie_file"))
                or resolve_path(_default_cookie_name(eff_user)))
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(parsed["cookie_str"] + "\n")
        acct["cookie_file"] = path
        cookie_saved = True
    elif cookie_paste.strip() and not parsed["xsrf"]:
        handler.send_json({"error": "没有解析到任何 cookie，也没找到 SNlM0e"}, 400)
        return

    CONFIG["accounts"] = accts
    try:
        save_config()
    except RuntimeError as e:
        handler.send_json({"error": str(e)}, 400)
        return
    handler.send_json({"ok": True, "idx": idx, "cookie_saved": cookie_saved,
                       "xsrf_saved": bool(xsrf)})


def _api_accounts_activate(handler):
    req = _read_json_body(handler) or {}
    accts = get_accounts()
    try:
        idx = int(req.get("idx"))
    except (TypeError, ValueError):
        handler.send_json({"error": "idx 必须是数字"}, 400)
        return
    if not (0 <= idx < len(accts)):
        handler.send_json({"error": f"账号不存在：{idx}"}, 400)
        return
    CONFIG["active_account"] = idx
    try:
        save_config()
    except RuntimeError as e:
        handler.send_json({"error": str(e)}, 400)
        return
    handler.send_json({"ok": True, "active_account": idx})


def _api_accounts_delete(handler):
    req = _read_json_body(handler) or {}
    accts = get_accounts()
    try:
        idx = int(req.get("idx"))
    except (TypeError, ValueError):
        handler.send_json({"error": "idx 必须是数字"}, 400)
        return
    if not (0 <= idx < len(accts)):
        handler.send_json({"error": f"账号不存在：{idx}"}, 400)
        return
    acct = accts.pop(idx)
    removed_file = False
    if req.get("remove_file"):
        path = resolve_path(acct.get("cookie_file"))
        if path and os.path.exists(path):
            os.remove(path)
            removed_file = True
    active = CONFIG.get("active_account") or 0
    if not accts:
        CONFIG["active_account"] = 0
    elif idx == active:
        CONFIG["active_account"] = 0
    elif idx < active:
        CONFIG["active_account"] = active - 1
    try:
        save_config()
    except RuntimeError as e:
        handler.send_json({"error": str(e)}, 400)
        return
    handler.send_json({"ok": True, "removed": True, "removed_file": removed_file,
                       "active_account": CONFIG["active_account"]})


def _api_test(handler):
    req = _read_json_body(handler)
    model = str(req.get("model") or CONFIG.get("default_model") or "gemini-3.5-flash")
    cookie_str, sapisid = load_cookie()
    t0 = time.time()
    try:
        _, mode_id, think_mode, err, extra = resolve_model(model)
        if err:
            handler.send_json({"error": err}, 400)
            return
        text = generate("用一句话介绍你自己", mode_id, think_mode, extra_fields=extra)
    except urllib.error.HTTPError as e:
        detail = f"上游 HTTP {e.code}"
        if e.code == 400 and cookie_str:
            detail = ("上游 HTTP 400：该账号的 cookie 可能被拒绝（失效/导出不完整/触发风控）。"
                      "请重新导出 cookie，或补一个 xsrf_token（SNlM0e）")
        handler.send_json({"ok": False, "detail": detail, "latency_ms": int((time.time()-t0)*1000)}, 200)
        return
    except Exception as e:
        handler.send_json({"ok": False, "detail": f"{type(e).__name__}: {e}", "latency_ms": int((time.time()-t0)*1000)}, 200)
        return
    handler.send_json({"ok": bool(text and text.strip()), "model": model,
                       "anonymous": not bool(cookie_str),
                       "text": (text or "")[:500], "latency_ms": int((time.time()-t0)*1000)})


# ─── H5 console page ─────────────────────────────────────────────────────────

def handle_ui(handler) -> bool:
    """Serve the console page at /ui; redirect browsers from / to /ui."""
    import urllib.parse
    parsed = urllib.parse.urlparse(handler.path)
    if parsed.path == "/ui":
        body = PAGE_HTML.encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        return True
    return False


PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gemini-web2api 控制台</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #f1f5f9; color: #1e293b; line-height: 1.6; }
  header { background: linear-gradient(135deg, #1d4ed8, #3b82f6); color: #fff; padding: 20px 24px; }
  header h1 { font-size: 20px; font-weight: 600; }
  header .sub { opacity: .85; font-size: 13px; margin-top: 2px; }
  main { max-width: 860px; margin: 0 auto; padding: 20px 16px 60px; }
  .card { background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          padding: 20px; margin-top: 16px; }
  .card h2 { font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 10px; }
  .item { background: #f8fafc; border-radius: 8px; padding: 10px 12px; font-size: 13px; }
  .item .k { color: #64748b; font-size: 12px; }
  .item .v { font-weight: 600; word-break: break-all; }
  .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .b-gray { background: #e2e8f0; color: #475569; } .b-green { background: #dcfce7; color: #15803d; }
  .b-yellow { background: #fef9c3; color: #a16207; } .b-red { background: #fee2e2; color: #b91c1c; }
  .b-blue { background: #dbeafe; color: #1d4ed8; }
  label { display: block; font-size: 13px; color: #475569; margin: 12px 0 4px; }
  input[type=text], input[type=number], select, textarea {
    width: 100%; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 8px;
    font-size: 14px; font-family: inherit; background: #fff; }
  textarea { min-height: 110px; resize: vertical; font-family: Consolas, monospace; font-size: 12.5px; }
  input:focus, select:focus, textarea:focus { outline: 2px solid #93c5fd; border-color: #3b82f6; }
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .row > * { flex: 1; min-width: 160px; }
  .btn { border: none; border-radius: 8px; padding: 9px 18px; font-size: 14px; cursor: pointer; font-weight: 600; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .btn-primary { background: #2563eb; color: #fff; }
  .btn-primary:hover:not(:disabled) { background: #1d4ed8; }
  .btn-ghost { background: #e2e8f0; color: #334155; }
  .btn-danger { background: #fee2e2; color: #b91c1c; }
  .actions { display: flex; gap: 10px; margin-top: 14px; flex-wrap: wrap; }
  .hint { font-size: 12px; color: #64748b; margin-top: 4px; }
  .result { margin-top: 12px; padding: 10px 12px; border-radius: 8px; font-size: 13px; display: none; }
  .result.show { display: block; }
  .r-ok { background: #f0fdf4; border: 1px solid #bbf7d0; color: #166534; }
  .r-err { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; }
  .r-info { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; }
  details { margin-top: 12px; font-size: 13px; color: #475569; }
  summary { cursor: pointer; font-weight: 600; color: #2563eb; }
  ol { margin: 8px 0 0 20px; } li { margin: 4px 0; }
  code, pre { font-family: Consolas, "Courier New", monospace; font-size: 12.5px; }
  pre { background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 8px; overflow-x: auto; position: relative; margin-top: 6px; }
  .copy-btn { position: absolute; top: 6px; right: 6px; background: #334155; color: #e2e8f0;
              border: none; border-radius: 6px; padding: 3px 10px; font-size: 12px; cursor: pointer; }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
  .tab { padding: 5px 14px; border-radius: 999px; background: #e2e8f0; cursor: pointer; font-size: 13px; border: none; }
  .tab.active { background: #2563eb; color: #fff; }
  #toast { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
           background: #0f172a; color: #fff; padding: 10px 22px; border-radius: 999px;
           font-size: 14px; display: none; z-index: 99; box-shadow: 0 4px 12px rgba(0,0,0,.3); }
  .client-block { margin-top: 12px; }
  .client-block .t { font-size: 13px; font-weight: 600; color: #334155; }
  .log-box { background: #0f172a; color: #cbd5e1; border-radius: 8px; padding: 10px 12px;
             margin-top: 10px; max-height: 320px; overflow-y: auto;
             font-family: Consolas, "Courier New", monospace; font-size: 12.5px; line-height: 1.5; }
  .log-line { white-space: pre-wrap; word-break: break-all; padding: 1px 0; }
  .log-line .t { color: #64748b; margin-right: 6px; }
  .log-line.lv-error { color: #fca5a5; }
  .log-box .empty { color: #64748b; }
  .zone-title { font-size: 14px; font-weight: 700; color: #334155;
                margin: 26px 0 0 4px; display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .zone-title .zone-sub { font-size: 12px; font-weight: 400; color: #94a3b8; }
  .link-btn { margin-left: auto; background: none; border: none; color: #2563eb;
              font-size: 13px; font-weight: 600; cursor: pointer; padding: 2px 6px; }
  .link-btn:hover { text-decoration: underline; }
  .card.collapsed { padding: 16px 20px; }
  .card.collapsed #ck-body { display: none; }
  .acct-row { border: 1px solid #e2e8f0; border-radius: 8px; margin-bottom: 8px; overflow: hidden; }
  .acct-row.open { border-color: #94a3b8; overflow: visible; }
  .acct-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
               padding: 10px 12px; background: #f8fafc; }
  .acct-head b { font-size: 13px; }
  .acct-actions { margin-left: auto; display: flex; gap: 10px; }
  .acct-body { padding: 12px; border-top: 1px solid #e2e8f0; }
  .acct-body label { margin-top: 8px; }
  .link-btn.danger { color: #dc2626; }
  .empty { color: #94a3b8; font-size: 12px; padding: 8px 0; }
  .card.collapsed .hint { margin-bottom: 0; }
  .tip { display:inline-flex; align-items:center; justify-content:center; width:15px; height:15px;
         border-radius:50%; background:#e2e8f0; color:#64748b; font-size:11px; font-weight:600;
         cursor:help; margin-left:6px; vertical-align:middle; position:relative; user-select:none; }
  .tip:hover { background:#cbd5e1; color:#0f172a; }
  .tip:hover::after { content: attr(data-tip); position:absolute; left:0; top:21px; width:260px;
         max-width:260px; background:#0f172a; color:#f8fafc; border-radius:8px; padding:9px 11px;
         font-size:12px; font-weight:400; line-height:1.55; text-align:left; z-index:99;
         box-shadow:0 4px 14px rgba(15,23,42,.22); }
  .kv { font-size:12px; color:#64748b; margin:4px 0 0; word-break:break-all; }
  .kv b { color:#0f172a; font-weight:500; }
</style>
</head>
<body>
<header>
  <h1>gemini-web2api 控制台 <span class="badge b-blue" id="hdr-ver"></span></h1>
  <div class="sub">OpenAI 兼容 API · <span id="hdr-addr"></span></div>
</header>
<main>

  <section class="card">
    <h2>📊 服务状态 <span id="st-badge" class="badge b-gray">加载中…</span></h2>
    <div class="grid" id="status-grid"></div>
  </section>

  <div class="zone-title">① 连接与账号 <span class="zone-sub">决定“能否连上 Gemini、以什么身份访问”</span></div>

  <section class="card">
    <h2>👥 账号（多用户）
      <span id="acct-badge" class="badge b-gray">未配置</span>
      <span class="tip" data-tip="一个 Google 账号 = 一个 auth_user(/u/N) + 一个 cookie + 一个 xsrf_token，三者配对使用。请求被拒(400/401/403/429)时自动切换到下一个启用账号。一个账号都不配 = 匿名模式，Flash 系列照常可用。">?</span>
    </h2>
    <div id="acct-list"><div class="empty">（加载中…）</div></div>
    <div class="actions">
      <button class="btn btn-primary" id="btn-acct-add">+ 新增账号</button>
      <button class="btn btn-ghost" onclick="window.open('https://gemini.google.com','_blank')">打开 Gemini 登录页 ↗</button>
    </div>
    <div class="result" id="acct-result"></div>
    <details>
      <summary>如何获取 cookie 和 xsrf_token？</summary>
      <ol>
        <li>打开 <b>gemini.google.com</b> 并登录（Pro 路由需 Gemini Advanced 付费账号）。多账号时看地址栏的 <code>/u/0</code>、<code>/u/1</code> 确定序号。</li>
        <li><b>cookie</b>：<code>F12</code> → <code>Application</code>(应用) → <code>Cookies</code> → <code>https://gemini.google.com</code>，复制整串粘贴即可（也支持请求头、Copy as fetch）。</li>
        <li><b>xsrf_token</b>（可选）：在页面按 <code>Ctrl+U</code> 看源代码，<b>整页复制粘进下面的 xsrf 框</b>，会自动提取 <code>SNlM0e</code>。多数情况下留空即可；若请求被拒（400）再补它。</li>
        <li>两者可以分开填、分开保存，也可以一次把整页源码粘进任一框。</li>
      </ol>
    </details>
  </section>

  <section class="card">
    <h2>🌐 网络与鉴权
      <span class="tip" data-tip="代理决定能否访问 gemini.google.com（国内直连会超时）；API Key 决定谁能调用本服务，留空则不校验。">?</span>
    </h2>
    <label>代理（国内必配，如 http://127.0.0.1:7897）</label>
    <input type="text" id="cf-proxy" placeholder="留空则用系统环境变量">
    <label>API Keys（每行一个；留空则不校验密钥）</label>
    <textarea id="cf-keys" style="min-height:60px" placeholder="sk-xxxxxxxx"></textarea>
  </section>

  <div class="zone-title">② 模型与请求行为 <span class="zone-sub">决定“用哪个模型、失败后怎么重试”</span></div>

  <section class="card">
    <h2>🤖 默认模型 <span class="badge b-blue" id="cf-model-cur">—</span>
      <span class="tip" data-tip="兜底模型：客户端没传 model、或传了未知模型名(如 gemini-9.9)时回退到它。客户端显式指定的模型优先级更高。">?</span>
    </h2>
    <select id="cf-model"></select>
    <div class="row" style="margin-top:12px">
      <div><label>请求超时（秒）</label><input type="number" id="cf-timeout"></div>
      <div><label>重试次数</label><input type="number" id="cf-retry"></div>
      <div><label>重试间隔（秒）</label><input type="number" id="cf-retry-delay"></div>
    </div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px">
      <input type="checkbox" id="cf-log"> 记录请求日志
    </label>
    <details>
      <summary>进阶：gemini_bl（前端版本号）</summary>
      <p class="hint">服务启动时会自动向 Google 抓取最新版本号；仅在自动更新失败时才需要手工填写。</p>
      <input type="text" id="cf-bl">
    </details>
    <div class="actions">
      <button class="btn btn-primary" id="btn-save-cfg">保存配置</button>
      <span class="hint" id="cfg-saved-hint"></span>
    </div>
  </section>

  <section class="card">
    <h2>🧪 连通性测试 <span class="badge b-gray">一次性验证</span>
      <span class="tip" data-tip="只验证当前配置能否正常出结果，这里选的模型不会被保存。">?</span>
    </h2>
    <div class="row">
      <div><label>用这个模型测试</label><select id="ts-model"></select></div>
      <div style="display:flex;align-items:flex-end"><button class="btn btn-primary" id="btn-test" style="width:100%">发送测试请求</button></div>
    </div>
    <div class="result" id="ts-result"></div>
  </section>

  <div class="zone-title">③ 观测与接入 <span class="zone-sub">排障、以及把服务接到客户端</span></div>

  <section class="card">
    <h2>📜 运行日志 <span class="badge b-gray" id="log-count">0 条</span>
      <span class="tip" data-tip="最近 500 条。每条请求打两行：→ 出发(账号、/u/N、model、prompt 长度、cookie 字段数、xsrf 有无)，← 回来(HTTP 码、耗时、响应长度)。账号切换、重试、BL 更新也会记。">?</span>
    </h2>
    <div style="display:flex;gap:12px;align-items:center;margin-top:8px;flex-wrap:wrap">
      <label style="display:flex;align-items:center;gap:6px;margin:0">
        <input type="checkbox" id="log-auto" checked> 每 2 秒自动刷新
      </label>
      <button class="btn btn-ghost" id="btn-log-refresh" style="padding:5px 14px">立即刷新</button>
      <button class="btn btn-danger" id="btn-log-clear" style="padding:5px 14px">清空</button>
    </div>
    <div id="log-box" class="log-box"><div class="empty">（暂无日志，发一次请求试试）</div></div>
  </section>

  <section class="card">
    <h2>🔌 客户端接入</h2>
    <div class="tabs">
      <button class="tab active" data-t="cherry">Cherry Studio / ChatBox</button>
      <button class="tab" data-t="curl">curl</button>
      <button class="tab" data-t="py">OpenAI SDK</button>
      <button class="tab" data-t="gemini">Gemini CLI</button>
    </div>
    <div id="client-body"></div>
  </section>

</main>
<div id="toast"></div>

<script>
const $ = id => document.getElementById(id);
let S = null;                 // status snapshot
let AUTH_KEY = localStorage.getItem('gw2a_key') || '';

function toast(msg, ms=2600) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', ms);
}
async function api(path, opts={}, timeoutMs=0) {
  opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  if (AUTH_KEY) opts.headers['Authorization'] = 'Bearer ' + AUTH_KEY;
  let timer = null;
  if (timeoutMs > 0) {
    const ctrl = new AbortController();
    opts.signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), timeoutMs);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } finally {
    if (timer) clearTimeout(timer);
  }
  if (r.status === 401) {
    const k = prompt('此服务已启用 API Key 鉴权，请输入密钥：');
    if (k) { AUTH_KEY = k; localStorage.setItem('gw2a_key', k); return api(path, opts); }
    throw new Error('未授权：需要 API Key');
  }
  return r.json();
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function showResult(el, ok, html) {
  el.className = 'result show ' + (ok === null ? 'r-info' : ok ? 'r-ok' : 'r-err');
  el.innerHTML = html;
}

async function loadStatus() {
  try { S = await api('/api/status'); } catch (e) { $('st-badge').textContent = e.message; return; }
  $('hdr-ver').textContent = 'v' + S.version;
  $('hdr-addr').textContent = S.base_url;
  $('st-badge').textContent = '运行中'; $('st-badge').className = 'badge b-green';

  const ck = S.cookie;
  const badge = $('acct-badge');
  if (!ck.configured) { badge.textContent = '未配置（匿名模式）'; badge.className = 'badge b-gray'; }
  else if (ck.missing.length) { badge.textContent = '已配置 · 缺少 ' + ck.missing.join(','); badge.className = 'badge b-yellow'; }
  else { badge.textContent = '已配置（6/6 完整）'; badge.className = 'badge b-green'; }

  const g = (k, v) => `<div class="item"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $('status-grid').innerHTML =
    g('监听地址', esc(S.host + ':' + S.port)) +
    g('代理', esc(S.proxy || '系统环境变量')) +
    g('默认模型', '<span id="st-model">' + esc(S.default_model) + '</span>') +
    g('API Key', S.auth_enabled ? `已启用（${S.api_keys_count} 个）` : '未启用（免密）') +
    g('流式输出', S.streaming ? 'httpx 真流式' : 'urllib 缓冲') +
    g('Cookie 状态', ck.configured ? esc(ck.hint || '已加载') : '匿名') +
    g('配置文件', esc(S.config_path || '未加载（仅默认值）')) +
    g('请求日志', S.log_requests ? '开启' : '关闭');

  // fill config form
  $('cf-proxy').value = S.proxy || '';
  $('cf-bl').value = S.gemini_bl || '';
  $('cf-timeout').value = 180; $('cf-retry').value = 3; $('cf-retry-delay').value = 2;
  $('cf-log').checked = !!S.log_requests;
  const modelSel = $('cf-model'), tsSel = $('ts-model');
  modelSel.innerHTML = ''; tsSel.innerHTML = '';
  Object.keys(S.models).forEach(m => {
    modelSel.add(new Option(m + ' — ' + S.models[m], m, false, m === S.default_model));
    tsSel.add(new Option(m, m, false, m === S.default_model));
  });
  $('cf-model-cur').textContent = '当前 ' + S.default_model;
  renderClient();
}

// ── accounts ──
// One row per Google account. Each owns an auth_user, a cookie and an
// xsrf_token; all three are edited independently inside the row.
let ACCTS = [];
let openRow = null;          // idx of the row whose editor is expanded

function acctName(a) {
  return a.auth_user === null || a.auth_user === '' ? '默认账号' : `/u/${a.auth_user}`;
}

function acctState(a) {
  // xsrf_token is optional: requests succeed without it unless Google
  // specifically challenges the session (then it answers 400 and we failover).
  if (!a.cookie_configured) return ['未配置 cookie（匿名）', 'b-gray'];
  if (a.missing && a.missing.length) return ['cookie 缺 ' + a.missing.join(','), 'b-yellow'];
  return [a.xsrf_set ? '就绪' : '就绪（未设 xsrf）', 'b-green'];
}

function renderAccounts() {
  const box = $('acct-list');
  if (!ACCTS.length) {
    box.innerHTML = '<div class="empty">（尚未配置任何账号，当前为匿名模式，Flash 系列可直接使用）</div>';
    return;
  }
  box.innerHTML = ACCTS.map(a => {
    const [txt, cls] = acctState(a);
    const open = openRow === a.idx;
    return `<div class="acct-row${open ? ' open' : ''}">
      <div class="acct-head">
        <b>${esc(a.label || acctName(a))}</b>
        ${a.active ? '<span class="badge b-green">当前</span>' : ''}
        <span class="badge ${cls}">${esc(txt)}</span>
        <span class="acct-actions">
          ${a.active ? '' : `<button class="link-btn" data-act="activate" data-i="${a.idx}">设为当前</button>`}
          <button class="link-btn" data-act="toggle" data-i="${a.idx}">${open ? '收起' : '编辑'}</button>
          <button class="link-btn danger" data-act="del" data-i="${a.idx}">删除</button>
        </span>
      </div>
      ${open ? `<div class="acct-body">
        <div class="row">
          <div><label>auth_user
            <span class="tip" data-tip="Google 账号序号，只能填数字。从浏览器地址栏看：gemini.google.com/u/1/... 里的 1 就是。无法从 cookie 反推，但单账号不用填——留空时请求不带 /u/N，实测可用。只在多账号时才需要逐个填。">?</span></label>
            <input type="text" data-f="auth_user" data-i="${a.idx}" value="${esc(a.auth_user ?? '')}" placeholder="留空 = 单账号"></div>
          <div><label>备注名</label>
            <input type="text" data-f="label" data-i="${a.idx}" value="${esc(a.label || '')}" placeholder="起个好认的名字"></div>
        </div>
        <label>cookie
          <span class="tip" data-tip="粘贴整串即可，也认请求头、Copy as fetch。自动提取 6 个关键字段：SID / HSID / SSID / APISID / SAPISID / __Secure-1PSID。直接粘整页源码也行，能同时把 xsrf 提出来。">?</span></label>
        <textarea data-f="cookie" data-i="${a.idx}" placeholder="SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx"></textarea>
        <p class="kv">${a.hint ? `已存：<b>${esc(a.hint)}</b>` : '未配置（匿名模式）'}</p>
        <div class="actions">
          <button class="btn btn-ghost" data-act="parse-cookie" data-i="${a.idx}">解析并保存 Cookie</button>
        </div>
        <label>xsrf_token
          <span class="tip" data-tip="页面里的 SNlM0e，可选。按 Ctrl+U 打开源码整页粘进来，自动提取。实测不带它请求也能成功，所以只在报 400 时才需要补。">?</span></label>
        <textarea data-f="xsrf" data-i="${a.idx}" style="min-height:50px" placeholder="粘贴 gemini.google.com 整页源码（Ctrl+U）即可"></textarea>
        <p class="kv">${a.xsrf_hint ? `已存：<b>${esc(a.xsrf_hint)}</b>` : '未设置（可选，多数情况不需要）'}</p>
        <div class="actions">
          <button class="btn btn-ghost" data-act="parse-xsrf" data-i="${a.idx}">解析并保存 xsrf</button>
        </div>
        <div class="actions">
          <button class="btn btn-primary" data-act="save" data-i="${a.idx}">保存该账号</button>
          <label style="display:flex;align-items:center;gap:6px;margin:0">
            <input type="checkbox" data-f="enabled" data-i="${a.idx}" ${a.enabled ? 'checked' : ''}> 启用（参与故障转移）
          </label>
        </div>
      </div>` : ''}
    </div>`;
  }).join('');
}

$('acct-list').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('[data-act]');
  if (!btn) return;
  const i = +btn.dataset.i, act = btn.dataset.act;
  if (act === 'toggle') { openRow = openRow === i ? null : i; renderAccounts(); return; }
  if (act === 'activate') {
    try { await api('/api/accounts/activate', {method: 'POST', body: JSON.stringify({idx: i})}); toast('已设为当前账号'); await loadAccounts(); loadStatus(); }
    catch (e) { toast('操作失败：' + e.message); }
    return;
  }
  if (act === 'del') {
    const a = ACCTS.find(x => x.idx === i) || {};
    if (!confirm(`确定删除账号 ${a.label || acctName(a)} 吗？\n（cookie 文件会一并删除，其它账号不受影响）`)) return;
    try { await api('/api/accounts', {method: 'DELETE', body: JSON.stringify({idx: i, remove_file: true})}); toast('已删除'); openRow = null; await loadAccounts(); loadStatus(); }
    catch (e) { toast('删除失败：' + e.message); }
    return;
  }
  if (act === 'parse-cookie' || act === 'parse-xsrf') {
    // Each field parses and persists on its own: pasting cookies must not
    // clear the xsrf token, and vice versa.
    const field = act === 'parse-cookie' ? 'cookie' : 'xsrf';
    const pick = f => { const el = document.querySelector(`[data-f="${f}"][data-i="${i}"]`); return el ? el.value : ''; };
    const raw = pick(field);
    if (!raw.trim()) { toast(`请先粘贴${field === 'cookie' ? ' cookie' : ' xsrf'}内容`); return; }
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = '解析中…';
    try {
      const body = {idx: i, auth_user: pick('auth_user'), label: pick('label')};
      body[field] = raw;
      const en = document.querySelector(`[data-f="enabled"][data-i="${i}"]`);
      body.enabled = en ? en.checked : true;
      const r = await api('/api/accounts', {method: 'POST', body: JSON.stringify(body)});
      let html = '<b>解析完成</b>';
      if (r.cookie_saved) html += '<br>✓ cookie 已写入文件';
      if (r.xsrf_saved) html += '<br>✓ xsrf_token 已写入';
      if (!r.cookie_saved && !r.xsrf_saved) html += '<br>⚠ 没有解析出有效内容，请检查粘贴的内容';
      showResult($('acct-result'), true, html);
      await loadAccounts(); loadStatus();
    } catch (e) { showResult($('acct-result'), false, esc(e.message)); }
    btn.disabled = false;
    btn.textContent = label;
    return;
  }
  if (act === 'save') {
    const val = f => { const el = document.querySelector(`[data-f="${f}"][data-i="${i}"]`); return el ? el.value : ''; };
    const checked = f => { const el = document.querySelector(`[data-f="${f}"][data-i="${i}"]`); return el ? el.checked : true; };
    btn.disabled = true;
    try {
      const r = await api('/api/accounts', {method: 'POST', body: JSON.stringify({
        idx: i, auth_user: val('auth_user'), label: val('label'),
        cookie: val('cookie'), xsrf: val('xsrf'), enabled: checked('enabled'),
      })});
      let html = '<b>已保存</b>';
      if (r.cookie_saved) html += '<br>✓ cookie 已写入文件';
      if (r.xsrf_saved) html += '<br>✓ xsrf_token 已写入';
      if (!r.cookie_saved && !r.xsrf_saved) html += '<br>（没有粘贴新内容，仅更新了账号信息）';
      showResult($('acct-result'), true, html);
      await loadAccounts(); loadStatus();
    } catch (e) { showResult($('acct-result'), false, esc(e.message)); }
    btn.disabled = false;
  }
});

$('btn-acct-add').onclick = () => {
  const next = ACCTS.length ? Math.max(...ACCTS.map(a => (typeof a.auth_user === 'number' ? a.auth_user : -1))) + 1 : 0;
  api('/api/accounts', {method: 'POST', body: JSON.stringify({auth_user: next, label: '', cookie: '', xsrf: '', enabled: true})})
    .then(() => loadAccounts())
    .then(() => { openRow = ACCTS.length - 1; renderAccounts(); toast('已新增账号，请粘贴 cookie 与 xsrf_token'); })
    .catch(e => toast('新增失败：' + e.message));
};

async function loadAccounts() {
  try {
    const r = await api('/api/accounts');
    ACCTS = r.accounts || [];
    renderAccounts();
  } catch (e) { $('acct-list').innerHTML = '<div class="empty">（读取账号失败：' + esc(e.message) + '）</div>'; }
}

// ── config ──
$('btn-save-cfg').onclick = async () => {
  const keys = $('cf-keys').value.split('\n').map(s => s.trim()).filter(Boolean);
  const body = {
    api_keys: keys,
    proxy: $('cf-proxy').value.trim() || null,
    default_model: $('cf-model').value,
    gemini_bl: $('cf-bl').value.trim() || null,
    request_timeout_sec: +$('cf-timeout').value || 180,
    retry_attempts: +$('cf-retry').value || 3,
    retry_delay_sec: +$('cf-retry-delay').value || 2,
    log_requests: $('cf-log').checked,
    // auth_user / xsrf_token now belong to an account, not to global config.
  };
  try {
    const r = await api('/api/config', {method: 'POST', body: JSON.stringify(body)});
    $('cfg-saved-hint').textContent = '已写入 ' + r.saved;
    $('cf-model-cur').textContent = '当前 ' + body.default_model;
    toast('配置已保存' + (r.auth_enabled ? '（API 鉴权已启用）' : '（免密）'));
    // keep the connectivity test aligned with the newly saved default model
    $('ts-model').value = body.default_model;
    loadStatus();
  } catch (e) { toast('保存失败：' + e.message); }
};

// ── test ──
$('btn-test').onclick = async () => {
  $('btn-test').disabled = true;
  showResult($('ts-result'), null, '请求中…');
  try {
    const r = await api('/api/test', {method: 'POST', body: JSON.stringify({model: $('ts-model').value})});
    if (r.ok) showResult($('ts-result'), true, `<b>${esc(r.model)}</b> · ${r.latency_ms} ms · ${r.anonymous ? '匿名' : '已认证'}<br>${esc(r.text)}`);
    else showResult($('ts-result'), false, `<b>失败</b>（${r.latency_ms} ms）：${esc(r.detail)}`);
  } catch (e) { showResult($('ts-result'), false, esc(e.message)); }
  $('btn-test').disabled = false;
};

// ── clients ──
let curTab = 'cherry';
document.querySelectorAll('.tab').forEach(t => t.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  t.classList.add('active'); curTab = t.dataset.t; renderClient();
});
function copyPre(btn) {
  const txt = btn.parentElement.querySelector('code').innerText;
  navigator.clipboard.writeText(txt).then(() => { btn.textContent = '已复制'; setTimeout(() => btn.textContent = '复制', 1500); });
}
function renderClient() {
  if (!S) return;
  const host = location.hostname, port = S.port, key = AUTH_KEY || (S.auth_enabled ? 'sk-你的密钥' : 'sk-anything');
  const base = `http://${host}:${port}/v1`;
  const blocks = {
    cherry: `<div class="client-block"><div class="t">Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端</div>
      <table style="font-size:13px;margin-top:6px;border-collapse:collapse">
      <tr><td style="padding:2px 10px 2px 0;color:#64748b">Base URL</td><td><code>${base}</code></td></tr>
      <tr><td style="padding:2px 10px 2px 0;color:#64748b">API Key</td><td><code>${esc(key)}</code></td></tr>
      <tr><td style="padding:2px 10px 2px 0;color:#64748b">模型</td><td><code>${esc(S.default_model)}</code></td></tr></table></div>`,
    curl: `<div class="client-block"><div class="t">curl 测试</div><pre><button class="copy-btn" onclick="copyPre(this)">复制</button><code>curl ${base}/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer ${esc(key)}" \\
  -d '{"model":"${esc(S.default_model)}","messages":[{"role":"user","content":"你好"}]}'</code></pre></div>`,
    py: `<div class="client-block"><div class="t">OpenAI Python SDK</div><pre><button class="copy-btn" onclick="copyPre(this)">复制</button><code>from openai import OpenAI
client = OpenAI(base_url="${base}", api_key="${esc(key)}")
resp = client.chat.completions.create(
    model="${esc(S.default_model)}",
    messages=[{"role": "user", "content": "解释量子计算"}],
)
print(resp.choices[0].message.content)</code></pre></div>`,
    gemini: `<div class="client-block"><div class="t">Gemini CLI（Google 原生端点）</div><pre><button class="copy-btn" onclick="copyPre(this)">复制</button><code>export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://${host}:${port}
gemini</code></pre></div>`,
  };
  $('client-body').innerHTML = blocks[curTab];
}

// ── logs ──
let logSeq = 0;
function appendLog(e) {
  const box = $('log-box'); if (!box) return;
  const empty = box.querySelector('.empty'); if (empty) empty.remove();
  const d = document.createElement('div');
  d.className = 'log-line lv-' + (e.level || 'info');
  d.innerHTML = `<span class="t">[${esc(e.t)}]</span>${esc(e.msg)}`;
  box.appendChild(d);
  while (box.children.length > 500) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  $('log-count').textContent = box.children.length + ' 条';
}
async function pollLogs() {
  try {
    const r = await api('/api/logs?since=' + logSeq);
    (r.logs || []).forEach(appendLog);
    if (r.seq > logSeq) logSeq = r.seq;
  } catch (e) { /* unauthorized: stay quiet, config cards still work */ }
}
$('btn-log-refresh').onclick = pollLogs;
$('btn-log-clear').onclick = async () => {
  try {
    await api('/api/logs', {method: 'DELETE'});
    $('log-box').innerHTML = '<div class="empty">（已清空，等待新日志…）</div>';
    $('log-count').textContent = '0 条';
    toast('日志已清空');
  } catch (e) { toast('清空失败：' + e.message); }
};
setInterval(() => { if ($('log-auto').checked) pollLogs(); }, 2000);

loadStatus().then(loadAccounts).then(pollLogs);
</script>
</body>
</html>
"""
