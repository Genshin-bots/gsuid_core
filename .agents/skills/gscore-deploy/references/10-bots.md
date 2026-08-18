# 十、链接 Bot 适配器清单

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[九、插件管理体系](./09-plugins.md) · **下一章**：[十一、数据库配置](./11-database.md)

GsCore 不是独立的聊天机器人，而是**业务核心**——必须配合上游 Bot 框架使用。
Bot 端作为 WS 客户端连 Core，Core 把业务逻辑跑完的结果回给 Bot 转发到平台。

> 用户文档：https://docs.sayu-bot.com/LinkBots/AdapterList.html

## 10.1 通信协议

```
Bot 框架（运行在某机器）                            Core（运行在某机器）
   │                                                  │
   ├─ WS: ws://HOST:PORT/<bot_id>?token=<WS_TOKEN> ──→ │
   │                                                  │
   │   ←────── JSON MessageReceive（消息事件）───     │
   │                                                  │
   ├─ 处理后发回 MessageSend ──────────────────────→   │
```

WS URL 模板：

```
ws://{HOST}:{PORT}/{BOT_ID}?token={WS_TOKEN}
```

- `HOST` / `PORT`：Core 的 `config.json`
- `BOT_ID`：自定义字符串，区分多 Bot
- `token`：必须等于 Core 的 `WS_TOKEN`

> 该接口**有限流**（5 连败 / 900s 封禁，见 [七、WebSocket 安全](./07-security-ws.md)）。
> 写自定义适配器时复用 Core 的限流常量（`SecurityManager.MAX_RETRIES`）。

## 10.2 已支持 Bot 清单

> 数据来源：[`GenshinUID-docs/docs/LinkBots/AdapterList.md`](../../../../../GenshinUID-docs/docs/LinkBots/AdapterList.md)

### 10.2.1 🤖 NoneBot2 [最推荐]

- Bot：https://github.com/nonebot/nonebot2
- 适配器：https://github.com/KimigaiiWuyi/GenshinUID/tree/v4-nonebot2
- 支持平台：OneBot (QQ) / OneBotV12 / RedProtocol（NTQQ）/ QQ频道 / 微信 (NtChat) / KOOK / Telegram / 飞书 / DoDo / 米游社大别野 / Discord …
- **环境变量**：

  ```ini
  # .env 或环境
  gsuid_core_host=127.0.0.1
  gsuid_core_port=8765
  gsuid_core_ws_token=<你的 WS_TOKEN>
  ```

> NoneBot2 是支持平台最全、生态最熟的，建议**首选**。

### 10.2.2 🤖 HoshinoBot

- Bot：https://github.com/Ice9Coffee/HoshinoBot
- 适配器：https://github.com/KimigaiiWuyi/GenshinUID/tree/v4-hoshino
- 平台：OneBot（QQ）
- 修改 `client.py:17` 的 `WS_TOKEN` 字段

### 10.2.3 🤖 AstrBot

- Bot：https://github.com/Soulter/AstrBot
- 适配器：https://github.com/KimigaiiWuyi/astrbot_plugin_gscore_adapter
- 平台：QQ / QQ频道 / Telegram / 微信 / 企微 / 飞书
- 自带 WebUI：在 AstrBot WebUI 里改 `WS_TOKEN` 字段

### 10.2.4 🤖 ZeroBot

- Bot：https://github.com/wdvxdr1123/ZeroBot
- 适配器：https://github.com/RemKeeper/GSUID_Utils_ZeroBot
- 平台：OneBot（QQ）

### 10.2.5 🤖 YunZai-Bot

- Bot：https://github.com/yoimiya-kokomi/Miao-Yunzai
- 适配器：https://gitee.com/xiaoye12123/ws-plugin
- 平台：QQ

### 10.2.6 🤖 Koishi

- Bot：https://github.com/koishijs/koishi
- 适配器：https://github.com/GithubCin/gscore-adapter
- 平台：Satori / OneBot 及各类

### 10.2.7 🤖 XYBotV2

- Bot：https://github.com/HenryXiaoYang/XYBotV2
- 适配器：https://github.com/qiye531667706/XYBotV2-GsCoreAdapter
- 平台：微信

### 10.2.8 协议端插件直连（无需 Python 框架）

- TypeScript：[napcat-plugin-gscore-adapter](https://github.com/xiowo/napcat-plugin-gscore-adapter) — NapCat 直接连 Core
- Java：[gs-core-adapter](https://gitee.com/WeekDragon/gs-core-adapter) — RedProtocol

## 10.3 同机部署 vs 跨机部署

### 10.3.1 同机

Bot 和 Core 都在同一台机器：

- Core 的 `WS_TOKEN` 可以留空（仅信任 `127.0.0.1`）
- `TRUSTED_IPS` 默认包含 `localhost / ::1 / 127.0.0.1`，够用

```json
{
  "HOST": "localhost",
  "PORT": "8765",
  "WS_TOKEN": "",
  "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"]
}
```

### 10.3.2 跨机（公网 / 内网）

**必须**做：

1. Core 这边把 `HOST` 改成 `0.0.0.0`（或 `all` / `none` / `dual`）
2. Core 这边配 `WS_TOKEN`（或在 `TRUSTED_IPS` 加 Bot 机器的 IP）
3. 防火墙放行 `8765/tcp`
4. Bot 端 URL 用 `ws://<Core 机器 IP>:8765/<bot_id>?token=<WS_TOKEN>`

**推荐叠加**：

- Nginx / Caddy 反代 + TLS（`wss://`）
- 限速器会自动按 IP 封禁，**Bot 端 token 错了会被 Core 拉黑**（详见 [七、§7.3](./07-security-ws.md#73-ip-失败封禁securitymanager)）

### 10.3.3 Docker 内 Bot + 外部 Core（或反之）

```yaml
# Bot 容器内要连 Core
environment:
  - gsuid_core_host=host.docker.internal  # 宿主机 IP
  - gsuid_core_port=8765
```

Core 的 `docker-compose.yml` 默认有：

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

确保 Bot 容器内能访问宿主机的 `8765`。

## 10.4 多 Bot 复用同一个 Core

```json
{
  "WS_TOKEN": "shared-secret",
  "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"]
}
```

每个 Bot 用不同 `bot_id`：

```
Bot A → ws://HOST:PORT/bot_a?token=shared-secret
Bot B → ws://HOST:PORT/bot_b?token=shared-secret
```

Core 内部按 `bot_id` 创建独立 `Bot` 实例：

- 数据隔离
- 不同平台的事件互不干扰
- 定时任务可针对单个 Bot 推送

## 10.5 适配器选择速查

| 平台 | 首选适配器 |
|------|------------|
| QQ（OneBot v11） | NoneBot2 / Hoshino / ZeroBot / YunZai |
| QQ（NTQQ RedProtocol） | NoneBot2 + RedProtocol / gs-core-adapter |
| QQ 频道 | NoneBot2 / AstrBot |
| Telegram | NoneBot2 / AstrBot |
| Discord | NoneBot2 |
| KOOK | NoneBot2 |
| 飞书 | NoneBot2 / AstrBot |
| DoDo | NoneBot2 |
| 米游社大别野 | NoneBot2 |
| 微信 | NoneBot2 (NtChat) / AstrBot / XYBotV2 |

## 10.6 验证连通

启动顺序：**先 Core，再 Bot**。

```sh
# 1. 启动 Core（看日志确认 8765 监听）
uv run core

# 2. Core 日志里看到 WS 服务启动
# 3. 启动 Bot（看 Bot 日志：连 Core 成功 / 失败）

# 4. 在 Bot 里发消息 → Core 日志应能看到处理记录
```

## 10.7 常见错误

| 现象 | 原因 |
|------|------|
| Bot 日志说「连 Core 失败」 | Core 没启 / 端口错 / 防火墙 |
| Bot 连上但 Core 日志说「1008 Policy Violation」 | `WS_TOKEN` 不一致 / 为空 |
| Bot 能收消息但 Core 不响应 | master 没配置 / 命令头 `command_start` 配置不一致 |
| Bot 日志说「找不到 host.docker.internal」 | Bot 容器没加 `extra_hosts` |
| WS 频繁断开 | 反代 `proxy_read_timeout` 太短 / 网络抖动 |
