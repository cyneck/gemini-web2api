# Contributing

感谢愿意一起把这个项目做得更稳。它是一个**逆向实现**：Google 随时可能改动网页端内部协议，所以"能跑"和"能长期跑"之间的距离，主要靠测试、日志和可回滚的小改动来填。

## 开发环境

```bash
git clone https://github.com/cyneck/gemini-web2api.git
cd gemini-web2api
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"       # httpx + ruff
```

## 提交前必须通过的检查

```bash
python -m compileall -q gemini_web2api gemini_web2api.py tests
ruff check --select E4,E7,E9,F --ignore F401 .
python -m unittest discover -s tests -t . -v
```

启动自检（改了 HTTP 层或配置时请手动跑一次）：

```bash
python -m gemini_web2api --port 18081 --host 127.0.0.1 &
curl -fsS http://127.0.0.1:18081/healthz
curl -fsS http://127.0.0.1:18081/v1/models | head -c 200
kill %1
```

## 代码约定

- **`gemini_web2api/` 是唯一实现**。`gemini_web2api.py` 只是兼容入口，不要在它里面加逻辑。
- 新增配置项时，同时更新：`config.py` 的 `DEFAULT_CONFIG`（含默认值注释）、`config.example.json`、README 的配置表、必要时加入 `_NUMERIC_BOUNDS` 或 `startup_warnings()`。
- 读取配置一律用 `config.get_int()` / `config.get_list()`，它们在非法值上回退到默认值，不要让手写的配置把请求处理炸掉。
- 网络相关的新代码必须显式设置超时，并在 `finally` 中关闭响应体。
- 涉及用户输入（URL、路径、请求体）时，先想清楚上限与来源校验：默认拒绝而非默认放行。
- 日志**绝不**输出 cookie、`xsrf_token` 或 api key 原文，只输出掩码与数量。
- 逆向下游协议的改动请附上"为什么这样解析"的注释与测试用例；无法验证的猜测不要合并。

## 测试要求

- 纯逻辑（协议解帧、字段解析、URL 校验、配置解析、账号表操作）必须有单元测试，放在 `tests/`，用 `unittest`，不要引入 mock 框架之外的依赖。
- HTTP 层用 `ThreadedServer(("127.0.0.1", 0), GeminiHandler)` 起真实端口，参考 `tests/test_modular_sync.py` 里的 `StreamingEndpointTests`。
- 测试里不要访问真实网络。

## 提交与 PR

- 一个 PR 只做一件事；提交信息建议用 Conventional Commits（`feat:` / `fix:` / `chore:` / `docs:` / `refactor:`），正文说明**现象 → 原因 → 修法**。
- PR 描述里请写清：影响了哪些端点/配置项、如何验证、有没有兼容性变化。
- 新增行为请同时更新 `CHANGELOG.md` 的 `Unreleased` 段。

## 反馈协议失效

如果 Gemini 网页端更新导致请求失败：

1. 先看日志里是否有 `BL auto-updated` 与 `HTTP 405`（通常是 `bl` 参数过期，程序会自动更新）。
2. 用「协议失效」Issue 模板提交，附上 `log_requests` 打开的日志（**请先自行脱敏**）与失败的端点和模型。

## 许可证

提交即表示同意以 MIT 许可证发布你的贡献。
