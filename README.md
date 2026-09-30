# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[中文文档](README_CN.md)

Convert Google Gemini's web interface into an OpenAI-compatible API. Zero cost, cross-platform, single file.

## Features

- **Optional API Keys**: no auth when `api_keys` is empty, OpenAI-style Bearer auth when configured
- **OpenAI Compatible**: Drop-in replacement for `/v1/chat/completions` and `/v1/models`
- **Tool Calling**: Full function calling support (OpenAI format)
- **Multiple Models**: Flash (3.6), Extended Thinking (20k+ char output), Pro, Auto, Lite
- **Thinking Depth**: Adjustable via `@think=N` suffix (0=deepest, 4=shallowest)
- **Web Search**: Built-in internet access (Gemini's native search)
- **Cross-Platform**: Pure Python, single optional dependency (`httpx` for streaming)
- **Streaming**: SSE streaming support via `httpx`, with a stall watchdog that aborts silent upstream connections
- **Codex CLI**: Responses API (`/v1/responses`) for OpenAI Codex integration
- **Gemini CLI**: Google native API (`/v1beta/models`) for Gemini CLI compatibility
- **Multi-account failover**: round-robin across Google accounts, switching on credential or quota errors
- **Cookie auto-renewal**: rotates `__Secure-1PSIDTS` server-side, so Linux/macOS/containers stay signed in without a browser
- **Robust framing**: decodes the `)]}'` + length-prefixed frame protocol, including records split across network chunks
- **Hardened by default**: loopback listener, body-size and download caps, SSRF checks on caller-supplied image URLs, constant-time API key comparison
- **Built-in console**: web UI at `/ui` for accounts, cookies, config and live logs

## Quick Start

```bash
pip install httpx
cp config.example.json config.json
python -m gemini_web2api
```

Server starts at `http://localhost:8081/v1` (console at `/ui`, health probe at
`/healthz`). The default listener is `127.0.0.1`; pass `--host 0.0.0.0` (or set
`host` in the config) to expose it, and set `api_keys` before you do.

## Client Configuration

### Cherry Studio / ChatBox / any OpenAI client

| Field | Value |
|-------|-------|
| Base URL | `http://localhost:8081/v1` |
| API Key | any `api_keys` value from `config.json`; anything if not configured |
| Model | `gemini-3.5-flash-thinking` |

### curl

#### bash / macOS / Linux

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.5-flash","messages":[{"role":"user","content":"Hello!"}]}'
```

#### PowerShell (Windows)

```powershell
curl.exe --% http://127.0.0.1:8081/v1/chat/completions -H "Content-Type: application/json" -H "Authorization: Bearer sk-your-key" -d "{\"model\":\"gemini-3.5-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello!\"}]}"
```

> Note: On Windows PowerShell, use `curl.exe` and `--%` so PowerShell does not reinterpret JSON quoting or curl options.

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "Explain quantum computing"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

Supports Google native API endpoints:
- `GET /v1beta/models` — list models
- `POST /v1beta/models/{model}:generateContent` — non-streaming
- `POST /v1beta/models/{model}:streamGenerateContent` — streaming (SSE)

## Available Models

| Model | Description | Output |
|-------|-------------|--------|
| `gemini-3.6-flash` | All-around model (latest) | ~12k chars |
| `gemini-3.5-flash` | Alias for gemini-3.6-flash | ~12k chars |
| `gemini-3.5-flash-thinking` | Extended thinking, longest output | **~20k chars** |
| `gemini-3.5-flash-thinking-lite` | Adaptive thinking depth | ~15k chars |
| `gemini-3.1-pro` | Advanced math & code (needs cookie) | ~12k chars |
| `gemini-auto` | Auto model selection | varies |
| `gemini-flash-lite` | Fastest answers, lightweight | ~10k chars |

### Thinking Depth

Append `@think=N` to any model name:

```
gemini-3.5-flash-thinking@think=0   # deepest (default)
gemini-3.5-flash-thinking@think=2   # medium
gemini-3.5-flash-thinking@think=4   # shallowest
```

## Optional: Cookie for Pro

Anonymous access works for all models, but `gemini-3.1-pro` routes to Flash without authentication. To get real Pro routing, you need a **Gemini Advanced (paid subscription)** account cookie:

```bash
python -m gemini_web2api --cookie-file cookie.txt
```

### How to get cookies

1. Open Chrome, go to [gemini.google.com](https://gemini.google.com) and sign in with a **Gemini Advanced** Google account
2. Open DevTools (F12) → Application → Cookies → `https://gemini.google.com`
3. Copy these cookie values: `SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`
4. Create `cookie.txt` in this format:

```
SID=your_sid_value; HSID=your_hsid_value; SSID=your_ssid_value; APISID=your_apisid_value; SAPISID=your_sapisid_value; __Secure-1PSID=your_1psid_value
```

Or use the JSON format:
```json
{"cookie": "SID=xxx; HSID=xxx; SSID=xxx; APISID=xxx; SAPISID=xxx; __Secure-1PSID=xxx", "sapisid": "your_sapisid_value"}
```

**Alternative (browser extension)**: Use any "Export Cookies" extension to export cookies for `gemini.google.com` in Netscape format, then convert to the single-line format above.

### Authenticated account path and XSRF token

If the signed-in Gemini page URL contains an account index, such as:

```
https://gemini.google.com/u/1/app/...
```

set `auth_user` to that index. Authenticated web requests may also require the page XSRF token. In the rendered Gemini page source, this token is exposed as `SNlM0e`; pass it as `xsrf_token` in `config.json`. The server sends it as the `at` form field.

Example:

```json
{
  "cookie_file": "/app/cookie.txt",
  "auth_user": "1",
  "xsrf_token": "AOOh0P...",
  "gemini_bl": "boq_assistant-bard-web-server_YYYYMMDD.xx_p0"
}
```

If authenticated requests return HTTP 400 with an `xsrf` error, refresh Gemini Web, update `xsrf_token`, and make sure `auth_user` matches the `/u/<index>/` part of the browser URL.

Pro routing requires **Gemini Advanced** (paid subscription). A free Google account cookie will authenticate but silently fall back to Flash.

## Configuration

Start from the template, then adjust:

```bash
cp config.example.json config.json
python -m gemini_web2api --config config.json
```

`config.example.json` carries every supported key with its default value. The
ones you are most likely to touch:

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `127.0.0.1` | Listener address. Loopback by default; exposing the service is a deliberate decision. |
| `port` | `8081` | Listener port. |
| `api_keys` | `[]` | `[]` disables auth. When set, `/v1/*` requires `Authorization: Bearer <key>`, `x-api-key: <key>`, `x-goog-api-key: <key>` or `?key=`. |
| `proxy` | `null` | HTTP/HTTPS/SOCKS proxy for every upstream call. |
| `default_model` | `gemini-3.6-flash` | Model used when the client omits `model`. |
| `accounts` / `active_account` | `[]` / `0` | Multiple Google accounts with automatic failover (see below). |
| `cookie_rotation` | `true` | Auto-renew `__Secure-1PSIDTS` via `accounts.google.com/RotateCookies`. |
| `cookie_rotation_min_interval_sec` | `60` | Minimum interval between rotations (the endpoint rate-limits aggressively). |
| `stream_stall_timeout_sec` | `120` | Abort a stream that has been silent for this long. `0` disables the watchdog. |
| `request_timeout_sec` | `180` | Per-read timeout for non-streaming upstream calls. |
| `retry_attempts` / `retry_delay_sec` | `3` / `2` | Retry budget for non-account failures. |
| `max_request_body_bytes` | `67108864` (64 MiB) | Reject larger request bodies with HTTP 413. |
| `max_media_fetch_bytes` | `52428800` (50 MiB) | Size cap for remote images supplied by a caller. |
| `image_fetch_timeout_sec` | `30` | Timeout for downloading a caller-supplied image URL. |
| `image_fetch_allow_private_hosts` | `false` | Allow remote images from private/loopback ranges (only for isolated networks). |
| `max_image_attachments` | `8` | Maximum images accepted per request. |
| `max_accounts` | `32` | Maximum number of configured accounts. |
| `client_socket_timeout_sec` | `300` | Drop a connection that makes no socket progress for this long (slow-client guard). `0` disables it. |
| `cors_origins` | `[]` | Origins allowed to call the API from a browser. Empty means "same-origin only". |
| `temporary_chats` | `false` | Use Gemini Web temporary chats instead of writing to account history. |
| `emit_reasoning` | `false` | Also return the model's thinking (`reasoning_content` / `thoughts`) when a thinking model is used. |
| `emit_generated_images` | `true` | Expose generated image URLs (`message.images` for chat, `fileData` parts for the native API). |
| `log_requests` / `log_level` | `true` / `info` | Console logging. Cookie and key values are never logged. |

Every numeric key is clamped to a documented range at read time, so a typo in a
hand-edited config degrades to the default instead of disabling a limit.

Multi-account configuration:

```json
{
  "accounts": [
    {"auth_user": 0, "label": "primary", "cookie_file": "cookie_u0.txt", "xsrf_token": null, "enabled": true},
    {"auth_user": 1, "label": "backup", "cookie_file": "cookie_u1.txt", "xsrf_token": null, "enabled": true}
  ],
  "active_account": 0
}
```

Requests start from `active_account` and move to the next enabled account when
Google rejects the credentials (400/401/403) or the quota is exhausted
(429/1037/1095). Before switching, the failing account's cookies are renewed
once if `cookie_rotation` is enabled.

> **Security**: with `api_keys` empty and a non-loopback `host`, anyone who can
> reach the port can spend your Google account quota and rewrite the console
> configuration. The server prints a warning at startup in that case. See
> [SECURITY.md](SECURITY.md) for the full checklist.

## Docker

The image runs unprivileged and reads its state from a mounted `/config`
directory. There is deliberately **no baked-in `config.json`**: the previous
image shipped the example config, which meant every container started with the
demo key `sk-gemini` on `0.0.0.0`.

```bash
cp config.example.json config.json
docker build -t gemini-web2api .
docker run -d --name gemini-web2api \
  -p 127.0.0.1:8081:8081 \
  -v "$PWD/config.json:/config/config.json" \
  -v "$PWD/cookie.txt:/config/cookie.txt" \
  gemini-web2api
```

Or use Docker Compose (same volume layout, already restricted to loopback):

```bash
cp config.example.json config.json
docker compose -f docker-compose.local.yml up -d
```

Set `"cookie_file": "/config/cookie.txt"` in `config.json`. Publishing the port
as `127.0.0.1:8081:8081` keeps the container reachable only from the host even
though it listens on `0.0.0.0` inside its own network namespace. If you need
cross-host access, publish `8081:8081` **and** set `api_keys`.

A `HEALTHCHECK` probes `/healthz`; `docker inspect --format '{{.State.Health.Status}}' gemini-web2api` reports it.

> **Note**: If you get empty responses (`content: null`) with Docker's default bridge network, switch to host networking: `docker run --network host ...` or add `network_mode: host` in your compose file. This is caused by Gemini's upstream rejecting requests from certain Docker NAT IP ranges.

## Proxy

If you cannot access `gemini.google.com` directly (connection timeout), configure a proxy:

**Method 1: CLI argument**
```bash
python -m gemini_web2api --proxy http://127.0.0.1:7890
```

**Method 2: config.json**
```json
{"proxy": "http://127.0.0.1:7890"}
```

**Method 3: Environment variable** (auto-detected)
```bash
export HTTPS_PROXY=http://127.0.0.1:7890
python -m gemini_web2api
```

Works with Clash, V2Ray, Shadowsocks, or any HTTP proxy.

## Tool Calling

```python
resp = client.chat.completions.create(
    model="gemini-3.5-flash",
    messages=[{"role": "user", "content": "What's the weather in Tokyo?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
        }
    }]
)
```

## Image Input

OpenAI-style multimodal messages are supported for Chat Completions and the
Responses API. Use either HTTP(S) image URLs or base64 data URLs:

```python
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image"},
            {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}
        ]
    }]
)
```

## Limitations

- **Image upload may require cookies**: Multimodal input uses Gemini Web's image upload endpoint. If anonymous upload fails, configure a Gemini cookie.
- **Not real Pro/Ultra**: Without a paid subscription cookie, `gemini-3.1-pro` routes to the same Flash model. The "Pro" label is a UI preference, not a backend model switch.
- **Single-turn only**: Each request is an independent conversation. Multi-turn context is simulated by including previous messages in the prompt (Gemini Web's own conversation/response ids are not replayed yet — see below).
- **Generated media is one-way**: image URLs produced by the model are returned
  (`message.images` for `/v1/chat/completions`, `fileData` parts for
  `/v1beta/models/*`); they are not downloaded or re-uploaded for you.
- **Rate limits**: Google may throttle high-frequency requests. The server retries automatically but sustained heavy use may be blocked.
- **Per-request usage is estimated**: token counts are derived from character
  counts (~4 chars/token), not from the upstream's own accounting.

### Known gaps (documented, not yet implemented)

- **Session continuation**: the payload's conversation/response-id slot
  (`inner[2]`) is sent empty, so every turn is a fresh conversation. Wiring it up
  requires validating the id sequence against a live account; guessing would
  risk breaking working requests, so it is deliberately left alone.
- **Attachment upload for the native API**: only inline/base64 and URL images are
  accepted today.
- `gemini_web2api/chromeauth`, `streamio` and `tokencount` equivalents from
  sibling projects (Chrome-cookie import, byte-level stream IO, exact token
  counting) are not part of this codebase.

## Requirements

- Python 3.8+ (CI covers 3.8, 3.10, 3.12, 3.13)
- `httpx` (`pip install httpx`) — used for streaming requests
- Network access to `gemini.google.com` (proxy/VPN may be needed in some regions)

## How It Works

This tool reverse-engineers Google Gemini's web StreamGenerate protocol. It sends requests to the same endpoint that the Gemini web app uses, converting between OpenAI's API format and Gemini's internal protobuf-like format.

The model selection is controlled by field `[79]` in the request payload, mapped from Gemini's frontend JavaScript source (`MODE_CATEGORY` enum).

Response decoding is the delicate part, and lives in `gemini_web2api/protocol.py`:

```text
)]}'

934
[["wrb.fr",null,"<payload json string>"]]
27
[["e",4,null,null,1]]
```

- The `)]}'` prefix is stripped once.
- Decimal lines are **length hints in UTF-16 code units** (an emoji counts as
  two), so the decoder slices by code units rather than Python characters —
  otherwise the stream desynchronises on the first non-BMP character.
- A record can span several network chunks and several records can arrive glued
  together, so decoding is incremental (`FrameDecoder.feed`) with a per-line
  fallback when a hint disagrees with the payload.
- Each `wrb.fr` payload carries reply candidates: text at `[1][0]`, completion
  flag at `[8][0] == 2`, thinking at `[37][0][0]`, web images at `[12][1]`,
  generated images at `[12][7]` / `[12][0]["8"]`. `record[5]` carries
  `BardErrorInfo` codes (1037 quota, 1052 model/header rejected, 1060 IP
  throttled, 1095 rate limited), which are mapped to public HTTP statuses and
  retry decisions.

Cookie renewal uses `POST https://accounts.google.com/RotateCookies` with the
same opaque body the web client sends; the response `Set-Cookie` values are
merged into the cookie file so containers and headless hosts keep working
without a browser.

## Acknowledgments

- Inspired by the open-source API proxy ecosystem

## License

MIT

---

## 致谢

本项目的开发 agent 能力由 [GenericAgent](https://github.com/lsdefine/GenericAgent) 提供。

### 🚩 友情链接

[![GenericAgent](https://img.shields.io/badge/Agent_Framework-GenericAgent-orange?style=for-the-badge&logo=github)](https://github.com/lsdefine/GenericAgent)
[![LinuxDo](https://img.shields.io/badge/社区-LinuxDo-blue?style=for-the-badge)](https://linux.do/)
