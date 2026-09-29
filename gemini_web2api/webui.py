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

from .config import CONFIG, save_config, config_path, resolve_path
from .models import MODELS, resolve_model
from .gemini import generate, load_cookie, HAS_HTTPX
from . import __version__

REQUIRED_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID"]
TEST_PROMPT = "Reply with the single word: OK"


# ─── cookie parsing ──────────────────────────────────────────────────────────

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
        return {"cookie_str": "", "sapisid": "", "found": [], "missing": list(REQUIRED_COOKIES), "xsrf": ""}

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

    pairs = {}
    for part in text.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            pairs[k.strip()] = v.strip()

    found = [k for k in REQUIRED_COOKIES if k in pairs and pairs[k]]
    missing = [k for k in REQUIRED_COOKIES if k not in found]
    cookie_str = "; ".join(f"{k}={pairs[k]}" for k in pairs)
    sapisid = pairs.get("SAPISID") or sapisid_hint or None

    # opportunistically pull an XSRF token (SNlM0e) if the page source is pasted
    m = re.search(r'SNlM0e["\']?\s*[:=]\s*["\']([^"\']+)["\']', text)
    if m:
        xsrf = m.group(1)

    return {"cookie_str": cookie_str, "sapisid": sapisid, "found": found, "missing": missing, "xsrf": xsrf}


def _mask_hint(cookie_str: str) -> str:
    pairs = [p.strip() for p in cookie_str.split(";") if "=" in p]
    hints = []
    for p in pairs:
        k, v = p.split("=", 1)
        if k in REQUIRED_COOKIES and v:
            hints.append(f"{k}={v[:4]}…{v[-4:]}" if len(v) > 8 else f"{k}={v[:2]}…")
    return "; ".join(hints)


def _cookie_file_path() -> str:
    return resolve_path(CONFIG.get("cookie_file") or "cookie.txt")


def _cookie_state() -> dict:
    cookie_str, sapisid = load_cookie()
    state = {
        "configured": bool(cookie_str),
        "sapisid": bool(sapisid),
        "path": os.path.abspath(_cookie_file_path()),
        "hint": _mask_hint(cookie_str) if cookie_str else "",
        "found": [], "missing": list(REQUIRED_COOKIES),
    }
    if cookie_str:
        pairs = dict(p.split("=", 1) for p in cookie_str.split("; ") if "=" in p)
        state["found"] = [k for k in REQUIRED_COOKIES if pairs.get(k)]
        state["missing"] = [k for k in REQUIRED_COOKIES if not pairs.get(k)]
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
    }
    if method == "DELETE" and path == "/api/cookie":
        _api_cookie_clear(handler)
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
    for f in _CONFIG_FIELDS:
        if f in req:
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
        handler.send_json({"valid": False, "found": [], "missing": REQUIRED_COOKIES,
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
        handler.send_json({"error": "没有解析到任何 cookie"}, 400)
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
    if not CONFIG.get("cookie_file"):
        CONFIG["cookie_file"] = path
        try:
            save_config()
        except RuntimeError:
            pass
    handler.send_json({"ok": True, "saved": os.path.abspath(path),
                       "found": parsed["found"], "hint": _mask_hint(parsed["cookie_str"]),
                       "xsrf": parsed["xsrf"]})


def _api_cookie_clear(handler):
    """Remove the cookie file but keep the configured cookie_file path intact."""
    path = _cookie_file_path()
    removed = False
    if os.path.exists(path):
        os.remove(path)
        removed = True
    handler.send_json({"ok": True, "removed": removed, "anonymous": True})


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
        handler.send_json({"ok": False, "detail": f"上游 HTTP {e.code}", "latency_ms": int((time.time()-t0)*1000)}, 200)
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

  <section class="card">
    <h2>🔑 Cookie 配置 <span id="ck-badge" class="badge b-gray">未配置</span></h2>
    <p class="hint">Cookie 用于解锁 <b>gemini-3.1-pro</b> 真实路由；不配置则以匿名模式使用 Flash 系列（零配置可用）。支持粘贴：Cookie 请求头、document.cookie、DevTools「Copy as fetch」整段代码。</p>
    <textarea id="ck-input" placeholder="粘贴 cookie 内容，例如：&#10;SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx"></textarea>
    <div class="actions">
      <button class="btn btn-ghost" id="btn-parse">解析并验证</button>
      <button class="btn btn-primary" id="btn-save-ck" disabled>保存 Cookie</button>
      <button class="btn btn-danger" id="btn-clear-ck">清除（回匿名模式）</button>
      <button class="btn btn-ghost" onclick="window.open('https://gemini.google.com','_blank')">打开 Gemini 登录页 ↗</button>
    </div>
    <div class="result" id="ck-result"></div>
    <details>
      <summary>如何获取 Cookie？</summary>
      <ol>
        <li>点击上方按钮打开 <b>gemini.google.com</b> 并登录（Pro 路由需 Gemini Advanced 付费账号）。</li>
        <li>按 <code>F12</code> 打开开发者工具 → <code>Application</code>(应用) → 左侧 <code>Cookies</code> → <code>https://gemini.google.com</code>。</li>
        <li>找到 <code>SID</code>，双击值列全选复制；回到本页粘贴即可（智能解析会自动从整段文本中提取全部 6 个关键 cookie）。</li>
        <li>如果登录态请求返回 400/xsrf 错误，把 Gemini 网页<b>源代码</b>（Ctrl+U 全选复制）粘贴进来，会自动提取 <code>SNlM0e</code> token。</li>
      </ol>
    </details>
    <div class="row">
      <div><label>auth_user（多账号时的 /u/N 序号，可空）</label><input type="text" id="cf-auth-user"></div>
      <div><label>xsrf_token（可选，SNlM0e 值）</label><input type="text" id="cf-xsrf"></div>
    </div>
  </section>

  <section class="card">
    <h2>⚙️ 通用配置</h2>
    <label>API Keys（每行一个；留空则不校验密钥）</label>
    <textarea id="cf-keys" style="min-height:60px" placeholder="sk-xxxxxxxx"></textarea>
    <div class="row">
      <div><label>代理（可空，如 http://127.0.0.1:7890）</label><input type="text" id="cf-proxy"></div>
      <div><label>默认模型</label><select id="cf-model"></select></div>
      <div><label>gemini_bl（前端版本号）</label><input type="text" id="cf-bl"></div>
    </div>
    <div class="row">
      <div><label>请求超时（秒）</label><input type="number" id="cf-timeout"></div>
      <div><label>重试次数</label><input type="number" id="cf-retry"></div>
      <div><label>重试间隔（秒）</label><input type="number" id="cf-retry-delay"></div>
    </div>
    <label style="display:flex;align-items:center;gap:8px;margin-top:14px">
      <input type="checkbox" id="cf-log"> 记录请求日志
    </label>
    <div class="actions">
      <button class="btn btn-primary" id="btn-save-cfg">保存配置</button>
      <span class="hint" id="cfg-saved-hint"></span>
    </div>
  </section>

  <section class="card">
    <h2>🧪 连通性测试</h2>
    <div class="row">
      <div><label>模型</label><select id="ts-model"></select></div>
      <div style="display:flex;align-items:flex-end"><button class="btn btn-primary" id="btn-test" style="width:100%">发送测试请求</button></div>
    </div>
    <div class="result" id="ts-result"></div>
  </section>

  <section class="card">
    <h2>📜 运行日志 <span class="badge b-gray" id="log-count">0 条</span></h2>
    <p class="hint">服务端最近 500 条日志（请求、重试、BL 自动更新、错误），排障直接看这里。</p>
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
let pendingCookie = null;     // parsed cookie awaiting save

function toast(msg, ms=2600) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', ms);
}
async function api(path, opts={}) {
  opts.headers = Object.assign({'Content-Type': 'application/json'}, opts.headers || {});
  if (AUTH_KEY) opts.headers['Authorization'] = 'Bearer ' + AUTH_KEY;
  const r = await fetch(path, opts);
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
  const badge = $('ck-badge');
  if (!ck.configured) { badge.textContent = '未配置（匿名模式）'; badge.className = 'badge b-gray'; }
  else if (ck.missing.length) { badge.textContent = '已配置 · 缺少 ' + ck.missing.join(','); badge.className = 'badge b-yellow'; }
  else { badge.textContent = '已配置'; badge.className = 'badge b-green'; }

  const g = (k, v) => `<div class="item"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $('status-grid').innerHTML =
    g('监听地址', esc(S.host + ':' + S.port)) +
    g('代理', esc(S.proxy || '系统环境变量')) +
    g('默认模型', esc(S.default_model)) +
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
  $('cf-auth-user').value = S.auth_user || '';
  $('cf-xsrf').value = '';
  $('cf-xsrf').placeholder = S.xsrf_token ? '当前：' + S.xsrf_token : '未设置';
  const modelSel = $('cf-model'), tsSel = $('ts-model');
  modelSel.innerHTML = ''; tsSel.innerHTML = '';
  Object.keys(S.models).forEach(m => {
    modelSel.add(new Option(m + ' — ' + S.models[m], m, false, m === S.default_model));
    tsSel.add(new Option(m, m, false, m === S.default_model));
  });
  renderClient();
}

// ── cookie ──
$('btn-parse').onclick = async () => {
  const v = $('ck-input').value;
  if (!v.trim()) { toast('请先粘贴 cookie 内容'); return; }
  $('btn-parse').disabled = true;
  try {
    const r = await api('/api/cookie/validate', {method: 'POST', body: JSON.stringify({cookie: v})});
    pendingCookie = r.valid ? v : null;
    $('btn-save-ck').disabled = !r.valid;
    let html = `<b>${esc(r.detail)}</b>`;
    if (r.found && r.found.length) html += `<br>已识别（${r.found.length}/6）：${esc(r.found.join(', '))}`;
    if (r.missing && r.missing.length) html += `<br>缺失：${esc(r.missing.join(', '))}`;
    if (r.xsrf) html += `<br>检测到 xsrf_token，保存后可自动填入`;
    showResult($('ck-result'), r.valid, html);
  } catch (e) { showResult($('ck-result'), false, esc(e.message)); }
  $('btn-parse').disabled = false;
};
$('btn-save-ck').onclick = async () => {
  if (!pendingCookie) return;
  $('btn-save-ck').disabled = true;
  try {
    const r = await api('/api/cookie', {method: 'POST', body: JSON.stringify({cookie: pendingCookie})});
    if (r.xsrf && !$('cf-xsrf').value) { $('cf-xsrf').value = r.xsrf; }
    toast('Cookie 已保存并立即生效');
    pendingCookie = null; $('ck-input').value = '';
    loadStatus();
  } catch (e) { toast('保存失败：' + e.message); }
  $('btn-save-ck').disabled = true;
};
$('btn-clear-ck').onclick = async () => {
  if (!confirm('确定清除 Cookie 并回到匿名模式吗？')) return;
  try { await api('/api/cookie', {method: 'DELETE'}); toast('已清除，当前匿名模式'); loadStatus(); }
  catch (e) { toast('操作失败：' + e.message); }
};

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
    auth_user: $('cf-auth-user').value.trim() || null,
    xsrf_token: $('cf-xsrf').value.trim() || null,
  };
  try {
    const r = await api('/api/config', {method: 'POST', body: JSON.stringify(body)});
    $('cfg-saved-hint').textContent = '已写入 ' + r.saved;
    toast('配置已保存' + (r.auth_enabled ? '（API 鉴权已启用）' : '（免密）'));
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

loadStatus().then(pollLogs);
</script>
</body>
</html>
"""
