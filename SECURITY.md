# Security Policy

## Reporting a vulnerability

请通过 GitHub 的私密报告通道提交：仓库 **Security → Report a vulnerability**。
如果无法使用该入口，可以开一个 Issue 说明"需要私下沟通"，**不要在公开 Issue 里贴 cookie、密钥或可复现的利用细节**。

反馈时请尽量包含：

- 版本（`python -m gemini_web2api --version` 或 `/healthz` 返回的 `version`）
- 运行方式（本地 / Docker / 反向代理）与监听地址
- 是否配置了 `api_keys`、是否对外开放
- 最小复现步骤与实际影响

一般情况下 72 小时内会给出初步确认。修复发布后会在 CHANGELOG 中致谢（除非你希望匿名）。

## Supported versions

只维护 `main` 分支的最新版本；旧版本请先升级再验证问题。

## Threat model

这个项目**是一个把 Gemini 网页版会话暴露成本地 API 的代理**。请在下列前提下评估风险：

| 边界 | 说明 |
| --- | --- |
| 默认监听 | `127.0.0.1:8081`，仅本机可达。改成 `0.0.0.0` 后，任何能访问该端口的人都能用你的 Google 账号额度，并可通过 `/api/*` 改写配置。 |
| 鉴权 | `api_keys` 为空时 `/v1/*` 与 `/api/*` 都不鉴权。对外开放时**必须**设置 `api_keys`，并配合 HTTPS 与来源限制。 |
| Cookie | cookie 文件等价于账号登录态。不要提交到仓库（`.gitignore` 已覆盖 `cookie*.txt` / `cookie*.json`），不要放进公开的镜像层。 |
| 控制台 | `/ui` 是无认证的静态页面，本身不含密钥；所有变更操作走 `/api/*`，需要 `api_keys`、同源 JSON 请求。 |
| 远程图片 | 由调用方提供的 URL 会被下载，因此已限制为公网地址（拒绝环回/私有/链路本地/保留网段）、限制大小与重定向跳数。若你确实需要抓内网图片，可显式打开 `image_fetch_allow_private_hosts`，但请确认调用方可信。 |
| 上游协议 | 逆向实现的稳定性依赖 Google 未公开接口，随时可能失效。协议变化不属于安全漏洞，请用「协议失效」模板反馈。 |

## Hardening checklist

对外开放前建议逐项确认：

1. `host` 保持 `127.0.0.1`，或用反向代理 + 防火墙限制来源；
2. `api_keys` 设置为 ≥24 位随机串（启动日志会对空/弱密钥给出警告）；
3. `cors_origins` 保持为空，除非确有浏览器跨域需求；
4. `log_requests` 按需开启——日志会记录账号、模型、提示词长度，但从不记录 cookie 值；
5. cookie 与配置文件权限收紧到仅属主可读写（Cookie 续签回写时会自动 `chmod 600`）；
6. `max_request_body_bytes` / `max_media_fetch_bytes` 按实际业务收紧，不要依赖默认的 64 MiB。
