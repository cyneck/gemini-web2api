"""Configuration management."""
import json
import os
import tempfile

MIB = 1024 * 1024

DEFAULT_CONFIG = {
    "port": 8081,
    # Loopback by default. Binding a public interface hands an unauthenticated
    # proxy (and the console's config API) to the whole network, so exposing the
    # service is a deliberate choice rather than the default.
    "host": "127.0.0.1",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "stream_stall_timeout_sec": 120,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "log_level": "info",
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
    # Extra response fields. Both are opt-in: strict OpenAI clients can reject
    # unknown keys, so widening the payload is the caller's decision.
    "emit_reasoning": False,
    "emit_generated_images": True,
    # Resource caps.
    "max_request_body_bytes": 64 * MIB,
    "max_media_fetch_bytes": 50 * MIB,
    "image_fetch_timeout_sec": 30,
    "max_image_attachments": 8,
    "cookie_rotation": True,
    "cookie_rotation_min_interval_sec": 60,
    "max_accounts": 32,
    # Cross-origin access is denied unless an origin is listed here.
    "cors_origins": [],
    # Multi-account: one Google account == one auth_user + one cookie + one
    # xsrf_token. The legacy single-value fields above are kept for
    # compatibility and folded into accounts[0] by migrate_legacy_account().
    "accounts": [],
    "active_account": 0,
}

# Fields that must stay inside a sane range; a typo here would otherwise turn
# into a silent "no request ever succeeds" or "no limit at all".
_NUMERIC_BOUNDS = {
    "port": (1, 65535),
    "retry_attempts": (1, 20),
    "retry_delay_sec": (0, 600),
    "request_timeout_sec": (1, 3600),
    "stream_stall_timeout_sec": (0, 3600),
    "max_request_body_bytes": (1024, 1024 * MIB),
    "max_media_fetch_bytes": (1024, 1024 * MIB),
    "image_fetch_timeout_sec": (1, 600),
    "max_image_attachments": (1, 64),
    "cookie_rotation_min_interval_sec": (0, 3600),
    "max_accounts": (1, 512),
}

CONFIG = dict(DEFAULT_CONFIG)

# Per-account fields stored in CONFIG["accounts"][i].
ACCOUNT_FIELDS = ("auth_user", "label", "cookie_file", "xsrf_token", "enabled")

_CONFIG_PATH = None
_CONFIG_DIR = None


def load_config(path: str = None, create: bool = False):
    """Load config from a JSON file.

    `create=True` materialises the file (from the current defaults) when it does
    not exist yet. Without it the console cannot persist anything, because
    save_config() refuses to write when no path is known -- which is exactly the
    situation a container hits on its first run with an empty volume.
    """
    global _CONFIG_PATH, _CONFIG_DIR
    if path:
        absolute = os.path.abspath(path)
        _CONFIG_PATH = absolute
        _CONFIG_DIR = os.path.dirname(absolute)
        if os.path.exists(absolute):
            with open(absolute) as f:
                CONFIG.update(json.load(f))
        elif create:
            directory = os.path.dirname(absolute)
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            save_config()
    migrate_legacy_account()
    return CONFIG


def get_int(key: str, fallback: int = 0) -> int:
    """Read an integer setting, clamped to its documented range.

    Config files are hand-edited, so a value that is missing, quoted, negative or
    absurd must degrade to something safe instead of raising deep inside a
    request handler (or disabling a limit entirely).
    """
    default = DEFAULT_CONFIG.get(key, fallback)
    raw = CONFIG.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    low, high = _NUMERIC_BOUNDS.get(key, (None, None))
    if low is not None:
        value = max(low, min(high, value))
    return value


def get_list(key: str) -> list:
    """Read a list setting, tolerating a single string or None."""
    raw = CONFIG.get(key)
    if raw is None:
        return list(DEFAULT_CONFIG.get(key) or [])
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def is_loopback_host(host: str) -> bool:
    """True when the listener only accepts local connections."""
    if not host:
        return False
    normalised = str(host).strip().strip("[]").lower()
    return normalised in ("127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1")


def startup_warnings() -> list:
    """Operator-facing problems worth shouting about before serving traffic."""
    warnings = []
    keys = [str(k) for k in get_list("api_keys") if str(k).strip()]
    public_bind = not is_loopback_host(CONFIG.get("host"))
    if public_bind and not keys:
        warnings.append(
            f"监听地址 {CONFIG.get('host')}:{CONFIG.get('port')} 对网络开放，但 api_keys 为空："
            "任何能访问该端口的人都可以调用 API 并改写控制台配置。"
            "请改用默认的 127.0.0.1，或设置 api_keys。")
    if public_bind and keys:
        warnings.append(
            f"监听地址 {CONFIG.get('host')}:{CONFIG.get('port')} 对网络开放，"
            "请确认已用防火墙/反向代理限制来源，并只通过 HTTPS 暴露。")
    weak = [k for k in keys if len(k) < 12 or k in ("sk-gemini", "changeme", "test")]
    if weak:
        warnings.append("api_keys 中存在过弱/示例密钥，建议替换为至少 24 位的随机串。")
    if not keys:
        warnings.append("api_keys 为空：本机进程可无认证访问 /v1 与 /api。")
    if CONFIG.get("image_fetch_allow_private_hosts"):
        warnings.append("image_fetch_allow_private_hosts=true：已放开远程图片的私有网段限制，"
                        "仅在隔离网内使用。")
    return warnings


def account_limit() -> int:
    return get_int("max_accounts")



# ─── accounts ────────────────────────────────────────────────────────────────

def migrate_legacy_account() -> bool:
    """Fold the legacy single-account fields into accounts[0].

    Idempotent: once "accounts" is non-empty this does nothing. The legacy
    fields are left in place so older tooling keeps working.
    """
    if CONFIG.get("accounts"):
        return False
    cookie_file = CONFIG.get("cookie_file")
    xsrf = CONFIG.get("xsrf_token")
    auth_user = CONFIG.get("auth_user")
    if not cookie_file and not xsrf and auth_user is None:
        return False
    CONFIG["accounts"] = [{
        "auth_user": auth_user,
        "label": "",
        "cookie_file": cookie_file,
        "xsrf_token": xsrf,
        "enabled": True,
    }]
    CONFIG["active_account"] = 0
    return True


def get_accounts() -> list:
    """Return the configured account list (never None)."""
    return CONFIG.get("accounts") or []


def get_account(auth_user=None) -> dict:
    """Return one account dict.

    auth_user=None means "the active account". Falls back to {} when no
    accounts are configured, in which case callers use the legacy fields.
    """
    accts = get_accounts()
    if not accts:
        return {}
    if auth_user is None:
        i = CONFIG.get("active_account") or 0
        return accts[i] if 0 <= i < len(accts) else {}
    for a in accts:
        if str(a.get("auth_user")) == str(auth_user):
            return a
    return {}


def account_index(auth_user=None) -> int:
    """Index of the active account, or None when none are configured."""
    accts = get_accounts()
    if not accts:
        return None
    if auth_user is None:
        i = CONFIG.get("active_account") or 0
        return i if 0 <= i < len(accts) else 0
    for i, a in enumerate(accts):
        if str(a.get("auth_user")) == str(auth_user):
            return i
    return None


def account_cookie_path(auth_user=None) -> str:
    """Cookie file for one account; falls back to the legacy cookie_file."""
    acct = get_account(auth_user)
    return acct.get("cookie_file") or CONFIG.get("cookie_file")


def config_path() -> str:
    """Return the path of the loaded config file (None if defaults only)."""
    return _CONFIG_PATH


def resolve_path(p: str = None) -> str:
    """Resolve a possibly relative path against the config file's directory.

    Without this, a relative "cookie.txt" resolves against the shell's cwd,
    so the service finds different files depending on where it was started.
    """
    if not p:
        return None
    if os.path.isabs(p):
        return p
    return os.path.join(_CONFIG_DIR or os.getcwd(), p)


def save_config() -> str:
    """Persist current CONFIG back to the loaded config file (atomically).

    Returns the path written. Raises RuntimeError if no config file was loaded.
    """
    if not _CONFIG_PATH:
        raise RuntimeError("no config file loaded; start with --config or place ./config.json")
    directory = os.path.dirname(os.path.abspath(_CONFIG_PATH))
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, _CONFIG_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return _CONFIG_PATH


def find_config():
    """Search for config file in standard locations."""
    for p in ["./config.json", os.path.expanduser("~/.config/gemini-web2api/config.json")]:
        if os.path.exists(p):
            return p
    return None
