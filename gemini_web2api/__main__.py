"""Entry point: python -m gemini_web2api"""
import argparse
import os
import signal
import sys
import threading

from . import __version__
from .config import CONFIG, find_config, get_int, is_loopback_host, load_config, startup_warnings
from .gemini import HAS_HTTPX, close_http_clients
from .models import MODELS
from .server import GeminiHandler, ThreadedServer


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemini-web2api",
        description="把 Gemini 网页版封装成 OpenAI / Claude / Google 兼容 API")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取配置或 8081）")
    parser.add_argument("--host", type=str, default=None,
                        help="监听地址；默认 127.0.0.1，对外开放需自行承担风险")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--cookie-file", type=str, default=None, help="cookie 文件路径")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP 代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--no-cookie-rotation", action="store_true",
                        help="禁用 Cookie 自动续签（__Secure-1PSIDTS 轮转）")
    parser.add_argument("--version", action="version", version=f"gemini-web2api {__version__}")
    return parser


def _describe(port: int) -> None:
    host = CONFIG.get("host")
    print(f"gemini-web2api v{__version__}")
    print(f"  监听:      http://{host}:{port}" + ("" if is_loopback_host(host) else "  ⚠ 已对网络开放"))
    print(f"  Base URL:  http://localhost:{port}/v1")
    print(f"  控制台:    http://localhost:{port}/ui")
    print(f"  健康检查:  http://localhost:{port}/healthz")
    print(f"  模型:      {', '.join(MODELS.keys())}")
    accounts = CONFIG.get("accounts") or []
    if accounts:
        enabled = [a for a in accounts if a.get("enabled", True)]
        print(f"  账号:      {len(enabled)}/{len(accounts)} 启用（多账号自动故障转移）")
    else:
        print(f"  Cookie:    {'已配置' if CONFIG.get('cookie_file') else '未配置（匿名模式，能力受限）'}")
    print(f"  Cookie 续签: {'启用' if CONFIG.get('cookie_rotation', True) else '禁用'}")
    print(f"  代理:      {CONFIG.get('proxy') or '未配置（走系统环境变量）'}")
    print(f"  流式:      {'httpx 真流式' if HAS_HTTPX else 'urllib 缓冲（建议安装 httpx）'}")
    print(f"  临时会话:  {'开启' if CONFIG.get('temporary_chats', False) else '关闭'}")
    print(f"  请求体上限: {get_int('max_request_body_bytes') // (1024 * 1024)} MiB"
          f"，远程图片上限: {get_int('max_media_fetch_bytes') // (1024 * 1024)} MiB"
          f"，停滞超时: {get_int('stream_stall_timeout_sec')}s")
    print(f"  认证:      {'已启用 api_keys' if CONFIG.get('api_keys') else '未启用（无鉴权）'}")
    for warning in startup_warnings():
        print(f"  ⚠ {warning}")
    print()


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = (args.config or os.environ.get("GEMINI_WEB2API_CONFIG") or find_config())
    if config_path:
        # An explicit --config is created when missing so the console can save.
        load_config(config_path, create=bool(args.config))

    if args.port:
        CONFIG["port"] = args.port
    if args.host:
        CONFIG["host"] = args.host
    if args.cookie_file:
        CONFIG["cookie_file"] = args.cookie_file
    if args.proxy:
        CONFIG["proxy"] = args.proxy
    if args.no_cookie_rotation:
        CONFIG["cookie_rotation"] = False

    try:
        port = int(CONFIG["port"])
    except (TypeError, ValueError):
        print(f"配置错误：port 必须是数字，当前为 {CONFIG.get('port')!r}", file=sys.stderr)
        return 2
    if not (1 <= port <= 65535):
        print(f"配置错误：port 必须在 1-65535 之间，当前为 {port}", file=sys.stderr)
        return 2
    host = str(CONFIG.get("host") or "127.0.0.1")

    try:
        server = ThreadedServer((host, port), GeminiHandler)
    except OSError as error:
        print(f"无法监听 {host}:{port}：{error}", file=sys.stderr)
        return 1

    _describe(port)

    stopping = threading.Event()

    def _shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        print(f"\n收到信号 {signum}，正在停止…", file=sys.stderr)
        # serve_forever() must not be called from the same thread that handles
        # the signal, so the shutdown runs in a helper thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signal_name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, signal_name, None)
        if handler is not None:
            try:
                signal.signal(handler, _shutdown)
            except (ValueError, OSError):
                # Not available on this platform (e.g. SIGTERM on some Windows
                # configurations): KeyboardInterrupt still works.
                pass

    exit_code = 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            close_http_clients()
        print("已停止。", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main() or 0)
