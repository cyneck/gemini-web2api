# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added

- **协议解帧重写**（`gemini_web2api/protocol.py`）：按 `)]}'` 前缀 + UTF-16 code unit 长度提示增量解帧，正确处理记录跨网络分片、多帧粘包与长度提示不一致；长度提示异常时自动降级为逐行扫描。
- **候选解析补全**（同文件）：新增思考链（`candidate[37]`）、生成图片（`candidate[12][7]` 与 `candidate[12][0]["8"]`）、网络图片（`candidate[12][1]`）、完成标记（`candidate[8][0] == 2`）与 `BardErrorInfo` 错误码映射（1037/1050/1052/1060/1095/1013）。
- **Cookie 自动续签**（`gemini_web2api/rotation.py`）：调用 `accounts.google.com/RotateCookies` 轮转 `__Secure-1PSIDTS`，合并 `Set-Cookie` 后按原格式回写 cookie 文件（保持 JSON 结构），按 60 秒节流。账号级 400/401/403 会先续签再重试同一账号，之后才切换账号。Linux / macOS / 容器环境因此不再依赖浏览器导出。
- **流式停滞看门狗**：流式请求使用独立 httpx 客户端，`stream_stall_timeout_sec`（默认 120，0 表示关闭）作为读空闲超时，上游静默时中断连接并转为可重试错误，避免工作线程被僵尸连接占死。
- **SSRF 防护与下载限长**（`gemini_web2api/netguard.py`）：远程图片 URL 拒绝环回/私有/链路本地/保留网段与云元数据主机名，逐跳校验重定向，限制大小与超时；`max_media_fetch_bytes`、`image_fetch_timeout_sec`、`image_fetch_allow_private_hosts` 可调。
- **请求体上限**：`max_request_body_bytes`（默认 64 MiB），Content-Length 与 chunked 两种写法都拦截，超限返回 413。
- **控制台 CSRF 防护**：`/api/*` 的状态变更请求要求同源且 `Content-Type: application/json`；CORS 改为按 `cors_origins` 白名单回显，不再无条件 `*`。
- **`/healthz`** 健康检查端点（返回版本与模型数量），Dockerfile 增加 `HEALTHCHECK`。
- **优雅退出**：`__main__` 捕获 SIGINT/SIGTERM，停止接收新请求并关闭连接池。
- **慢客户端防护**：连接级 socket 超时 `client_socket_timeout_sec`（默认 300 秒，`0` 表示关闭）。此前客户端发出半个请求头后停住就能一直占住一个工作线程，几十个这类连接即可让 API 停止响应；该超时同时覆盖 SSE 写出，不再读数据的客户端会被断开而不是把线程钉死在写阻塞上。
- **新增配置项**：`log_level`、`stream_stall_timeout_sec`、`client_socket_timeout_sec`、`emit_reasoning`、`emit_generated_images`、`cookie_rotation`、`cookie_rotation_min_interval_sec`、`max_request_body_bytes`、`max_media_fetch_bytes`、`image_fetch_timeout_sec`、`image_fetch_allow_private_hosts`、`max_image_attachments`、`max_accounts`、`cors_origins`。
- **可观测性**：日志改为线程安全环形缓冲 + 分级过滤；401、413、流式失败都会记录；上游调用的返回行会带上响应字数、思考字数与图片数。
- **测试**：新增协议解帧、候选解析、URL 安全、续签合并 `Set-Cookie`、请求体上限、控制台守卫、账号保存回滚、常数时间密钥比较等用例；CI 增加多 Python 版本测试、编译检查、ruff(pyflakes) 检查、启动自检与 wheel 构建安装验证。
- **开源配套**：`SECURITY.md`、`CONTRIBUTING.md`、`CHANGELOG.md`、Issue 模板（缺陷 / 协议失效）、Dependabot。

### Changed

- **消除双实现**：`gemini_web2api.py` 从 1108 行的第二份完整实现改为薄壳入口，逻辑统一到 `gemini_web2api/` 包。此前两套代码各有独立 `CONFIG`，导致 `--config` / `--cookie-file` / `--proxy` **对图片上传不生效**（图片请求匿名且不走代理），且单文件版不认识多账号配置。旧命令 `python gemini_web2api.py` 仍可用，推荐 `python -m gemini_web2api`。
- **默认只监听回环地址**：`host` 默认由 `0.0.0.0` 改为 `127.0.0.1`。对外开放需要显式设置，并在检测到"非回环 + 空 `api_keys`"时于启动时打印告警。
- **Docker 镜像**：不再把 `config.example.json` 烤成 `/app/config.json`（此前镜像内自带示例弱口令 `sk-gemini`），改为挂载 `/config`；容器以非 root 用户运行；启用 OCI 元数据标签。
- **API Key 比较**改用 `hmac.compare_digest`（UTF-8 字节比较，兼容非 ASCII 密钥），并遍历全部密钥避免时序泄漏。
- **`http.client`/`urllib` 资源管理**：所有响应体显式 `close()`，httpx 客户端按代理配置复用并设上限。
- Cookie 写盘失败不再让已成功的请求失败：只记录告警。
- `--host` 新增命令行参数；`--no-cookie-rotation` 可关闭续签。

### Fixed

- **幽灵账号**：`POST /api/accounts` 先 `append` 再校验，校验失败（缺 cookie、uid 非法、写盘失败）会留下内存中的半成品账号，并会在下一次保存时持久化。现在全部校验通过后才提交，保存失败会回滚账号表与新建的 cookie 文件。
- **账号故障转移被破坏**：`gemini.py` 在失败分支上读取 `e.code`，而 `ValueError`（cookie 含非法字符时 urllib 抛出）没有该属性，会抛 `AttributeError` 并中断整个账号轮询。改用统一的 `_http_status()`。
- **流式失败静默截断**：流式分支的异常只写日志，客户端收到看似正常结束的残缺回答。现在会在流内发送 `error` 帧，并把结束原因标记为非 `stop`。
- **短帧丢失**：旧解帧用 `len(line) < 200` 与 `len(payload) < 50` 作为过滤条件，短回答可能被整体丢弃。
- 长度提示按 Python 字符数切分导致含 emoji / 非 BMP 字符时错位，现按 UTF-16 code unit 计数。
- 图片上传数量与大小缺乏限制；`multimodal` 的页面令牌缓存与 cookie 缓存改为线程安全并设上限。
- 相对路径 `cookie_file` 的解析统一走配置目录，避免随启动工作目录漂移。

## [1.1.0] - 2026-09-29

### Added

- 多账号支持：`accounts`/`active_account`，每账号独立 `auth_user` + cookie + `xsrf_token`，请求失败自动切换。
- 控制台账号管理（新增/激活/删除/测试）。
- `temporary_chats` 开关，对应 Gemini 临时会话标记。

### Changed

- cookie 解析放宽：只要有 `SAPISID` 与任一登录态 cookie 就接受，不再因为缺少非关键字段拒绝可用 cookie。

### Fixed

- 控制台保存 cookie 时不再拒绝本可用的粘贴内容。

## [1.0.0] - 2026-08-14

- 首次发布：OpenAI `/v1/chat/completions`、`/v1/responses`、Google `/v1beta/models/*`、内置控制台、httpx 流式、工具调用桥接、图片输入（Scotty 上传）。

[Unreleased]: https://github.com/cyneck/gemini-web2api/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/cyneck/gemini-web2api/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/cyneck/gemini-web2api/releases/tag/v1.0.0
