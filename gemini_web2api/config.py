"""Configuration management."""
import json
import os
import tempfile

DEFAULT_CONFIG = {
    "port": 8081,
    "host": "0.0.0.0",
    "retry_attempts": 3,
    "retry_delay_sec": 2,
    "request_timeout_sec": 180,
    "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.6-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
    "temporary_chats": False,
    # Multi-account: one Google account == one auth_user + one cookie + one
    # xsrf_token. The legacy single-value fields above are kept for
    # compatibility and folded into accounts[0] by migrate_legacy_account().
    "accounts": [],
    "active_account": 0,
}

CONFIG = dict(DEFAULT_CONFIG)

# Per-account fields stored in CONFIG["accounts"][i].
ACCOUNT_FIELDS = ("auth_user", "label", "cookie_file", "xsrf_token", "enabled")

_CONFIG_PATH = None
_CONFIG_DIR = None


def load_config(path: str = None):
    """Load config from JSON file."""
    global _CONFIG_PATH, _CONFIG_DIR
    if path and os.path.exists(path):
        _CONFIG_PATH = path
        _CONFIG_DIR = os.path.dirname(os.path.abspath(path))
        with open(path) as f:
            CONFIG.update(json.load(f))
    migrate_legacy_account()
    return CONFIG


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
