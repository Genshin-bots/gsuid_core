# 七、WebSocket 安全

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[六、`core_config.json` 字段详解](./06-core-config-json.md) · **下一章**：[八、WebConsole](./08-webconsole.md)

Bot 端（NoneBot2 / AstrBot / Koishi / ...）与 Core 之间是 **WebSocket 长连接**。
安全模型围绕 `WS_TOKEN` + `TRUSTED_IPS` + IP 失败封禁三层防线实现。

## 7.1 鉴权流程（核心代码：[`core.py:115-141`](../../../gsuid_core/core.py)）

```
Bot → WS 连接  ws://HOST:PORT/<bot_id>?token=<WS_TOKEN>
  ↓
Core 提取 client_host, token
  ↓
1. sec_manager.is_banned(client_host)?
   是 → close(1008)，拒绝
  ↓
1b. bot_id == webconsole_livechat?
   是 → 必须 `session_store.verify(token)`（控制台登录会话）
        失败 close(1008)；**不**用核心 WS_TOKEN，本机 IP 也不能跳过
   否 → 继续适配器鉴权
  ↓
2. sec_manager.is_trusted(client_host)?
   是 → 放行，直接进入业务
  ↓
3. WS_SECRET_TOKEN 为空?
   是 → close(1008)，「未配置 WS_TOKEN，所有外网连接将被拒绝！」
  ↓
4. token == WS_SECRET_TOKEN?
   否 → record_failure() → close(1008)，「Token 错误！」
   是 → record_success() → 进入业务
```

> close code `1008` = Policy Violation（WebSocket 标准）。

## 7.2 公网部署

**必做项**（任意一项不满足，外网 WS 连接都会被拒绝）：

| 必做 | 配置 | 说明 |
|------|------|------|
| ✅ 监听 0.0.0.0 | `HOST=0.0.0.0`（或 `--host 0.0.0.0`） | 否则 WS 只监听本机 |
| ✅ 配 `WS_TOKEN` 或 `TRUSTED_IPS` | `config.json` | 二选一 |

```json
{
  "HOST": "0.0.0.0",
  "PORT": "8765",
  "WS_TOKEN": "<随机32位以上secret>",
  "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"]
}
```

**建议叠加**：

- 套 Nginx / Caddy 反向代理，开 TLS（Bot 适配器一般支持 `wss://`）
- 服务器防火墙只放行必要 IP（如 Bot 所在机器）
- 把 `WS_TOKEN` 视为密码，**不要写进 git**（用环境变量或部署平台的 secrets）

### 7.2.1 Nginx 反代 + TLS 示例

```nginx
server {
    listen 443 ssl;
    server_name core.example.com;

    ssl_certificate     /etc/letsencrypt/live/core.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/core.example.com/privkey.pem;

    # WS 升级
    location /ws/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 600s;
    }

    # WebConsole / HTTP API。必须覆盖客户端自己的 X-Forwarded-For。
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Bot 端 URL 填 `wss://core.example.com/<bot_id>?token=<WS_TOKEN>`。

## 7.3 IP 失败封禁（`SecurityManager`）

源码：[`gsuid_core/security_manager.py`](../../../gsuid_core/security_manager.py)

| 常量 | 值 | 含义 |
|------|----|------|
| `MAX_RETRIES` | `5` | 连续失败 5 次触发封禁 |
| `BAN_DURATION` | `900` | 封禁时长 900 秒（15 分钟） |

封禁触发后 Core 日志：

```
🔒️ [GsCore] 拒绝来自已封禁 IP 的连接: 1.2.3.4
🚨 [GsCore] 非法访问拒绝: IP=1.2.3.4, BotID=nb2
🚨 [GsCore] Token 错误!剩余尝试次数: 3
```

> **状态只保存在内存**，Core 重启后清空。

## 7.4 反向代理下的真实 IP

[`security_manager.py:get_client_ip()`](../../../gsuid_core/security_manager.py) 与 WebSocket 的 `is_trusted()` 共用当前内存里的 `TRUSTED_IPS`。地址先用 `ipaddress` 收成合法 IP，IPv4-mapped IPv6 收成 IPv4。`localhost` 不是 IP，写进列表也不参与匹配。

- 直连地址不在这份列表里：忽略 `X-Forwarded-For` 和 `X-Real-IP`，限流按直连地址算。
- 直连地址在列表里：Core 只读第一行 `X-Forwarded-For`。反代用 `proxy_set_header` 覆盖客户端自己的这一行，逗号链路写在这一行里。Core 从右往左取第一个合法且不在列表里的地址，直连地址算最右一跳。
- 这一行没有可用结果时，用同样的规则看第一行 `X-Real-IP`。带端口、带方括号或其它非法片段丢掉。

**部署提示**：Core 套反代时，把反代自己的出口 IP（通常是 `127.0.0.1`）留在 `TRUSTED_IPS`。上面的 `location /` 和 `/ws/` 都要写 `X-Forwarded-For $proxy_add_x_forwarded_for`。只写 `X-Real-IP` 时，客户端自己的 `X-Forwarded-For` 仍会传进来，并盖过 `X-Real-IP`。多级代理还要把每一跳的地址写进同一列表，否则限流停在最靠近 Core 的那一跳。不要把公网网段写进信任列表。

## 7.5 WS 端 vs WebConsole 端的限流器

源码同文件，还有 [`AuthRateLimiter`](../../../gsuid_core/security_manager.py)：

| 限流器 | 维度 | 阈值 | 用途 |
|--------|------|------|------|
| `SecurityManager` | 客户端 IP | 5 连败 / 900s 封禁 | WS 机器人连接 |
| `AuthRateLimiter` | 客户端 IP | 60s 滑动 10 次，5 连败封禁 900s | WebConsole 登录 / 注册 / 改密 |

二者**独立**：Web 端连续输错密码**不会**误封 Bot 的 WS 连接。

## 7.6 多 Bot 共用一个 Core

```json
{
  "WS_TOKEN": "shared-secret",
  "TRUSTED_IPS": ["127.0.0.1", "10.0.0.5", "10.0.0.6"]
}
```

每个 Bot 用不同的 `<bot_id>` 区分：

- Bot A：`ws://HOST:PORT/bot_a?token=shared-secret`
- Bot B：`ws://HOST:PORT/bot_b?token=shared-secret`

`gss.connect()`（[`core.py:144`](../../../gsuid_core/core.py)）会根据 `bot_id` 创建
独立的 `Bot` 实例，互不干扰。

## 7.7 配置文件改完必须重启

`WS_TOKEN` / `HOST` / `PORT` 在 `core.py` 启动时读一次，改完必须重启。
`TRUSTED_IPS` 在每次 HTTP 取客户端 IP 和每次 WebSocket 判断信任时读取当前内存配置；控制台保存后下一次请求或连接生效。手改 `config.json` 文件仍要重启：

```sh
core重启    # Bot 内 master 命令
# 或
systemctl restart gsuid_core
# 或
docker restart gsuid_core
```

## 7.8 常见错误排查

| 现象 | 原因 |
|------|------|
| Bot 端一直连不上，Core 日志报 `1008 Policy Violation` | `WS_TOKEN` 不一致 / 为空 |
| Bot 能连但频繁断 | 反代超时太短 / `proxy_read_timeout` 过小 |
| 配了 Token 但仍连不上 | Bot URL 没带 `?token=` 参数 |
| 公网连不上 | `HOST=localhost` 没改 / 防火墙 / ISP 封端口 |
| 被 1008 拒绝且日志说「所有外网连接将被拒绝」 | `WS_TOKEN` 为空且不在 `TRUSTED_IPS` |
