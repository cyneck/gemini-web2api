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
    "gemini_bl": "boq_assistant-bard-web-server_20260525.09_p0",
    "auth_user": None,
    "xsrf_token": None,
    "default_model": "gemini-3.5-flash",
    "log_requests": True,
    "cookie_file": None,
    "proxy": None,
    "api_keys": [],
}

CONFIG = dict(DEFAULT_CONFIG)

_CONFIG_PATH = None


def load_config(path: str = None):
    """Load config from JSON file."""
    global _CONFIG_PATH
    if path and os.path.exists(path):
        _CONFIG_PATH = path
        with open(path) as f:
            CONFIG.update(json.load(f))
    return CONFIG


def config_path() -> str:
    """Return the path of the loaded config file (None if defaults only)."""
    return _CONFIG_PATH


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
