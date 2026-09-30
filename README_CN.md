# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API. 零成本, 跨平台, 单文件.

## 特性

- **可选密钥**: `api_keys` 为空时免密, 填入密钥后使用常数时间比较校验 Bearer / 头部 / query 三种携带方式
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash (3.6), 扩展思考 (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **多账号故障转移**: 多账号轮询, 凭证失效或额度耗尽自动切换下一个
- **Cookie 自动续签**: 服务端轮转 `__Secure-1PSIDTS`, Linux / macOS / 容器无需浏览器导出
- **跨平台**: 纯 Python, 仅一个可选依赖 (`httpx` 用于流式输出)
- **流式输出**: 基于 `httpx` 的 SSE Streaming, 带停滞看门狗 (上游静默自动中断)
- **稳健解帧**: 支持 `)]}'` + 长度前缀的帧协议, 正确处理跨分片与粘包
- **默认安全**: 默认只监听回环地址, 请求体与下载限长, 远程图片 URL 做 SSRF 校验
- **内置控制台**: `/ui` 管理账号、Cookie、配置与实时日志
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

```bash
pip install httpx
cp config.example.json config.json
python -m gemini_web2api
```

服务启动在 `http://localhost:8081/v1`（控制台 `/ui`，健康检查 `/healthz`）。
默认只监听 `127.0.0.1`；需要对外开放时用 `--host 0.0.0.0` 或在配置里设置 `host`，
并务必先配置 `api_keys`。

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | `config.json` 中的任意 `api_keys`；未配置时随便填 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.6-flash` | 全能模型 (最新) | ~1.2万字 |
| `gemini-3.5-flash` | gemini-3.6-flash 别名 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 扩展思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | 高级数学与代码 (需 cookie) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 最快响应, 轻量 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## 可选: Cookie 配置 (Pro 模型)

匿名访问对所有模型有效, 但 `gemini-3.1-pro` 在无认证时会路由到 Flash. 要获得真正的 Pro 路由, 需要 **Gemini Advanced (付费订阅)** 账号的 cookie:

```bash
python -m gemini_web2api --cookie-file cookie.txt
```

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录 **Gemini Advanced** 付费账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 复制以下 cookie 值: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. 创建 `cookie.txt`, 格式如下:

```
SID=你的SID值; HSID=你的HSID值; SSID=你的SSID值; APISID=你的APISID值; SAPISID=你的SAPISID值; __Secure-1PSID=你的1PSID值
```

或使用 JSON 格式:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "你的SAPISID值"}
```

**替代方案 (浏览器扩展)**: 使用任意 "Export Cookies" 扩展导出 `gemini.google.com` 的 cookie, 然后转换为上述单行格式.

### 登录账号路径与 XSRF Token

如果已登录的 Gemini 页面 URL 带账号序号, 例如:

```
https://gemini.google.com/u/1/app/...
```

请把 `auth_user` 设置为该序号。登录态的 Gemini Web 请求还可能需要页面里的 XSRF token。该 token 在渲染后的 Gemini 页面源码中名为 `SNlM0e`; 在 `config.json` 中填入 `xsrf_token` 后, 服务会把它作为 `at` 表单字段提交。

示例:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 `xsrf_token`, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

## 配置文件

从模板开始：

```bash
cp config.example.json config.json
python -m gemini_web2api --config config.json
```

`config.example.json` 列出了全部配置项及其默认值。最常调整的：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `host` | `127.0.0.1` | 监听地址。默认仅本机可达，对外开放需显式修改。 |
| `port` | `8081` | 监听端口。 |
| `api_keys` | `[]` | 空数组则不校验；填入后 `/v1/*` 需要 `Authorization: Bearer <key>`、`x-api-key`、`x-goog-api-key` 或 `?key=`。 |
| `proxy` | `null` | 全局 HTTP/HTTPS/SOCKS 代理。 |
| `default_model` | `gemini-3.6-flash` | 客户端未指定 model 时使用。 |
| `accounts` / `active_account` | `[]` / `0` | 多账号配置与起始账号，失败自动切换。 |
| `cookie_rotation` | `true` | 自动轮转 `__Secure-1PSIDTS` 续签 Cookie。 |
| `cookie_rotation_min_interval_sec` | `60` | 续签最小间隔（该端点会限流，不宜更小）。 |
| `stream_stall_timeout_sec` | `120` | 流式读空闲超时，上游静默超过该时长即中断；`0` 表示关闭看门狗。 |
| `request_timeout_sec` | `180` | 非流式上游请求的单次读超时。 |
| `retry_attempts` / `retry_delay_sec` | `3` / `2` | 非账号类失败的退避重试预算。 |
| `max_request_body_bytes` | `67108864`（64 MiB） | 超过则直接返回 413。 |
| `max_media_fetch_bytes` | `52428800`（50 MiB） | 调用方传入的远程图片大小上限。 |
| `image_fetch_timeout_sec` | `30` | 下载远程图片的超时。 |
| `image_fetch_allow_private_hosts` | `false` | 是否允许抓取内网图片地址（仅隔离网使用）。 |
| `max_image_attachments` | `8` | 单次请求的图片数量上限。 |
| `max_accounts` | `32` | 账号数量上限。 |
| `client_socket_timeout_sec` | `300` | 连接在此时长内没有任何 socket 进展就断开（慢客户端防护）；`0` 表示关闭。 |
| `cors_origins` | `[]` | 允许浏览器跨域调用的来源；留空表示仅同源。 |
| `temporary_chats` | `false` | 使用临时会话，不写入账号历史。 |
| `emit_reasoning` | `false` | 使用思考模型时额外返回思考链（`reasoning_content` / `thoughts`）。 |
| `emit_generated_images` | `true` | 暴露生成图片地址（chat 的 `message.images`，原生 API 的 `fileData` 部分）。 |
| `log_requests` / `log_level` | `true` / `info` | 日志开关与级别；日志不会输出 cookie 或密钥原文。 |

所有数值项在读取时都会做范围钳制：手写配置写错时回退到默认值，而不是把某项限流悄悄关掉。

多账号配置示例：

```json
{
  "accounts": [
    {"auth_user": 0, "label": "主号", "cookie_file": "cookie_u0.txt", "xsrf_token": null, "enabled": true},
    {"auth_user": 1, "label": "备用", "cookie_file": "cookie_u1.txt", "xsrf_token": null, "enabled": true}
  ],
  "active_account": 0
}
```

请求从 `active_account` 开始，遇到凭证被拒（400/401/403）或额度耗尽（429/1037/1095）
会自动切到下一个启用的账号。切换前，如果开启 `cookie_rotation`，会先对该账号做一次
Cookie 续签并重试。

> **安全提醒**：`api_keys` 为空且监听非回环地址时，任何能访问该端口的人都能消耗你的
> Google 账号额度，并通过控制台接口改写配置。这种情况下启动会打印告警。完整清单见
> [SECURITY.md](SECURITY.md)。

## Docker 部署

镜像以非 root 用户运行，状态统一放在挂载的 `/config` 目录下。镜像内**不再内置
`config.json`**：此前镜像会把示例配置烤进 `/app/config.json`，导致每个容器都带着示例弱口令
`sk-gemini` 监听 `0.0.0.0`。

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api \
  -p 127.0.0.1:8081:8081 \
  -v "$PWD/config.json:/config/config.json" \
  -v "$PWD/cookie.txt:/config/cookie.txt" \
  gemini-web2api
```

或使用 Docker Compose（卷布局相同，已限制为回环映射）：

```bash
cp config.example.json config.json
docker compose -f docker-compose.local.yml up -d
```

此时 `config.json` 中设置 `"cookie_file": "/config/cookie.txt"`。容器内部监听
`0.0.0.0` 是网络命名空间的需要；对外暴露由 `-p 127.0.0.1:8081:8081` 控制。若确实需要
跨机访问，请改成 `8081:8081` **并**配置 `api_keys`。

镜像内置 `HEALTHCHECK`，可用 `docker inspect --format '{{.State.Health.Status}}' gemini-web2api` 查看。

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...` 或在 compose 文件中添加 `network_mode: host`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python -m gemini_web2api --proxy http://127.0.0.1:7890
```

**方式 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**方式 3: 环境变量** (自动检测)
```bash
set HTTPS_PROXY=http://127.0.0.1:7890
python -m gemini_web2api
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 图片输入

Chat Completions 和 Responses API 支持 OpenAI 风格的多模态消息。图片可以使用
HTTP(S) URL 或 base64 data URL:

```python
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## 已知限制

- **图片上传可能需要 Cookie**: 多模态输入使用 Gemini 网页端图片上传接口。匿名上传失败时, 请配置 Gemini cookie。
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型. "Pro" 只是 UI 偏好标签.
- **单轮对话**: 每次请求是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟（Gemini 网页端自己的会话/响应 ID 尚未回放，见下方"已知缺口"）。
- **生成媒体是单向的**: 模型产出的图片地址会返回（chat 用 `message.images`，原生 API 用 `fileData` 部分），但不会替你下载或重新上传。
- **频率限制**: Google 可能限制高频请求, server 会自动重试但持续高负载可能被封.
- **用量是估算值**: token 数按字符数（约 4 字符/token）推算，不是上游真实计量。

### 已知缺口（已记录，尚未实现）

- **会话续接**: payload 的会话/响应 ID 槽位（`inner[2]`）目前发送空值，因此每一轮都是新会话。
  接上它需要用有效账号验证 ID 序列；靠猜测去改会破坏当前可用的请求，所以刻意不做。
- **原生 API 的附件上传**: 目前只接受内联 base64 与 URL 图片。
- 兄弟项目里的 Chrome Cookie 导入（`chromeauth`）、字节级流 IO（`streamio`）、精确 token 计数
  （`tokencount`）等能力不在本仓库内。

## 系统要求

- Python 3.8+（CI 覆盖 3.8 / 3.10 / 3.12 / 3.13）
- `httpx` (`pip install httpx`) — 用于流式请求
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

响应解码是最容易出错的部分，实现位于 `gemini_web2api/protocol.py`：

```text
)]}'

934
[["wrb.fr",null,"<payload json string>"]]
27
[["e",4,null,null,1]]
```

- `)]}'` 前缀只剥离一次；
- 十进制行是**以 UTF-16 code unit 计的长度提示**（一个 emoji 算两个），所以按 code unit
  切分而不是按 Python 字符切分，否则遇到非 BMP 字符后整个流会错位；
- 一条记录可能跨多个网络分片，多帧也可能粘在一起，因此解帧是增量的
  （`FrameDecoder.feed`），并在长度提示与内容不符时降级为逐行扫描；
- 每个 `wrb.fr` 载荷里携带回复候选：文本在 `[1][0]`，完成标记 `[8][0] == 2`，思考链在
  `[37][0][0]`，网络图片在 `[12][1]`，生成图片在 `[12][7]` / `[12][0]["8"]`；
  `record[5]` 携带 `BardErrorInfo` 错误码（1037 额度、1052 模型/请求头被拒、1060 IP 被限、
  1095 限流），代码里映射为对外状态码与重试策略。

Cookie 续签使用 `POST https://accounts.google.com/RotateCookies`（请求体与网页端一致的不透明
字符串），把响应里的 `Set-Cookie` 合并回 cookie 文件，因此容器与无浏览器主机也能长期保持登录。

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT
