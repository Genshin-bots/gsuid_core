# 五、`config.json` 字段详解

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[四、配置体系总览](./04-config-overview.md) · **下一章**：[六、`core_config.json` 字段详解](./06-core-config-json.md)

`data/config.json` 是 Core 自身的全局配置，由 [`gsuid_core/config.py`](../../../gsuid_core/config.py)
的 `CoreConfig` 类加载。字段定义见 [`CONFIG_DEFAULT`](../../../gsuid_core/config.py#L21-L41)。

## 5.1 默认值一览

```json
{
  "HOST": "localhost",
  "PORT": "8765",
  "ENABLE_HTTP": false,
  "WS_TOKEN": "",
  "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"],
  "masters": [],
  "superusers": [],
  "REGISTER_CODE": "<首次启动随机生成的 32 位 hex>",
  "misfire_grace_time": 90,
  "log": {
    "level": "INFO",
    "output": ["stdout", "stderr", "file"],
    "module": false
  },
  "enable_empty_start": true,
  "command_start": [],
  "buffered_user_writes": false,
  "sv": {}
}
```

## 5.2 字段逐项说明

### 5.2.1 `HOST` — 监听地址

| 取值 | 行为 |
|------|------|
| `localhost`（默认） | 仅本机可连 WS / WebConsole |
| `0.0.0.0` | 监听全部网卡（**公网部署必须**） |
| `127.0.0.1` / `::1` | 等价 localhost |
| `dual` / `none` / `all` / 空串 | 等价 `0.0.0.0`（代码映射） |

> Docker 部署时，宿主映射 `-p 8765:8765` 后还需要 `HOST=0.0.0.0`（镜像默认已经
> 设好，见 `Dockerfile` 的 `CMD ["uv", "run", "...", "core", "--host", "0.0.0.0"]`）。

### 5.2.2 `PORT` — 监听端口

- 默认 `8765`。
- 类型为**字符串**（保留兼容历史）。
- 范围限制：Linux 需 root 才能用 < 1024 端口。

### 5.2.3 `ENABLE_HTTP` — HTTP 模式开关

- `false`（默认）：只暴露 WS（`/ws/{bot_id}`）+ WebConsole（`/api/...`）。
- `true`：额外暴露 `POST /api/send_msg` 接口，传入 `MessageReceive` JSON 即可触发
  Core 内部消息处理（详见 [`core.py:199-210`](../../../gsuid_core/core.py)）。

```json
{ "ENABLE_HTTP": true }
```

> 启用 HTTP 后请**配合 WS_TOKEN** 或在反代层做 IP 白名单。

### 5.2.4 `WS_TOKEN` — WebSocket 鉴权 Token

Bot 连 Core 时在 query 里带 `token=<WS_TOKEN>`。

```json
{ "WS_TOKEN": "my-strong-random-secret" }
```

**生成**：

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**校验流程**（见 `core.py:115-141`）：

1. 提取 `websocket.client.host`
2. `sec_manager.is_trusted(client_host)` → 在 `TRUSTED_IPS` 直接放行
3. 否则检查 `token == WS_SECRET_TOKEN`
4. 5 次连错 → 封禁 900s（`SecurityManager.MAX_RETRIES=5, BAN_DURATION=900`）

> 与 Bot 端配置必须**完全一致**。详见 [七、WebSocket 安全](./07-security-ws.md)。

### 5.2.5 `TRUSTED_IPS` — 信任 IP 列表

```json
{
  "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1", "192.168.1.0/24"]
}
```

支持 IPv4 / IPv6 / CIDR。`SecurityManager.is_trusted()` 仅做**精确匹配**（CIDR
解析需要自己扩 `security_manager.py`，一般建议用反代层做 IP 段控制）。

**反向代理场景**：

如果 Core 跑在 Nginx / Caddy 后面，且想让 Core 通过 `X-Forwarded-For` 拿到真实
客户端 IP，需把**反向代理自己的 IP**加到 `TRUSTED_IPS`。`security_manager.py`
里的 `get_client_ip()` 仅在 `client_host in TRUSTED_IPS` 时才信任 `X-Forwarded-For`。
否则 `X-Forwarded-For` 会被忽略，用直连 IP 做限流。

### 5.2.6 `masters` — 主人账号列表（pm=0）

```json
{ "masters": ["444835641", "123456789"] }
```

- 类型：`List[str]`
- 对应权限等级 `pm=0`，最高权限。
- 填**平台原生用户 ID**（QQ 号、Telegram ID、Discord ID、Kaiheila ID...）。
- 不确定时可发一条消息到 Bot，看 Core 日志里的 `user_id` 字段。

### 5.2.7 `superusers` — 超级用户列表（pm=1）

```json
{ "superusers": ["987654321"] }
```

- 类型：`List[str]`
- 对应权限等级 `pm=1`。

### 5.2.8 `REGISTER_CODE` — WebConsole 注册码

- 首次启动随机生成 32 位 hex（`secrets.token_hex(16)`）。
- 注册 WebConsole 管理员账号时用。
- **只能注册一个管理员账号**；注册后建议从 WebConsole 改密码而不是改这个码。
- 忘了可以删 `data/web_user.db`（如果有独立库）或删 `GsData.db` 里 `web_user`
  表后重启，码会重新生成。

### 5.2.9 `misfire_grace_time` — 定时任务超时容差

- 默认 `90`（秒）。
- APScheduler 在 Job 错过执行时间（如 Core 在指定时间挂掉）后，仍允许补跑的秒数。

### 5.2.10 `log` — 日志配置

```json
{
  "log": {
    "level": "INFO",                       // DEBUG / INFO / WARNING / ERROR
    "output": ["stdout", "stderr", "file"],// 输出位置
    "module": false                        // 是否按模块分文件
  }
}
```

- `file` 输出在 `data/logs/`，保留 8 天（`ScheduledCleanLogDay` 默认 8）。
- 反馈 Bug 时把 `level` 改成 `"DEBUG"`，会刷出大量细节。

### 5.2.11 `enable_empty_start` — 空消息启动开关

- `true`（默认）：Core 启动时允许没有插件、没有 Bot 连接。
- `false`：未配插件或无 Bot 时拒绝启动。

### 5.2.12 `command_start` — 命令头

```json
{ "command_start": [""] }       // 等价 []
{ "command_start": ["*"] }      // 所有命令必须带 *
{ "command_start": ["/", "!"] } // / 或 ! 开头
```

- 类型：`List[str]`
- 留空 / `[""]` / `[]` = 无命令头（默认）。
- **填了之后所有命令都必须带命令头**才能触发，否则 `gs帮助` 都打不出来。

### 5.2.13 `buffered_user_writes` — 用户写入缓冲

- 默认 `false`。
- 主要影响数据库写入的批量合并策略，高并发时再开。

### 5.2.14 `sv` — 服务（触发器）权限矩阵

```json
{
  "sv": {
    "Core管理": {
      "priority": 5,
      "enabled": true,
      "pm": 0,
      "black_list": [],
      "area": "ALL",
      "white_list": []
    }
  }
}
```

每个服务的子字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `priority` | int | 数值越大越优先匹配 |
| `enabled` | bool | 是否启用（false 后命令不再响应） |
| `pm` | int | 权限等级：`0`=主人 / `1`=超管 / `2`=群主 / `3`=群管理 / `4`=频道管理 / `5`=子频道管理 / `6`=普通用户 |
| `black_list` | List[str] | 用户 / 群 黑名单 |
| `white_list` | List[str] | 白名单（仅这些用户 / 群可用） |
| `area` | str | 响应范围：`"ALL"` / `"GROUP"` / `"DIRECT"` |

> 每个插件启动时会把自己的 SV 注册到这里；WebConsole 改完大多数可热更。

### 5.2.15 `plugin_config_store`（运行时对象，非 JSON 字段）

`plugins_configs/` 目录下每个插件一份 JSON，由 `PluginConfigStore` 管理（见
[`config.py:194-307`](../../../gsuid_core/config.py)）。**不是 `config.json` 的字段**，
但概念上属于 Core 全局配置的"插件分支"。

详细规范：[十一、数据库配置](./11-database.md) 与插件自身文档。

## 5.3 PM 等级速查

| pm | 含义 |
|----|------|
| 0 | 主人（`masters`） |
| 1 | 超级用户（`superusers`） |
| 2 | 群主 / 频道主 |
| 3 | 群管理员 |
| 4 | 频道管理员 |
| 5 | 子频道管理员 |
| 6 | 普通用户 |

> 部分 WebConsole 版本把 `masters` 显示为「管理员列表」——这里的"管理员"指**主人**
> (`pm=0`)，不是群管理员。

## 5.4 完整示例（最小化部署）

```json
{
  "HOST": "0.0.0.0",
  "PORT": "8765",
  "ENABLE_HTTP": false,
  "WS_TOKEN": "你自己生成的32位secret",
  "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1", "192.168.1.0/24"],
  "masters": ["你的QQ号"],
  "superusers": [],
  "REGISTER_CODE": "首次启动后自动生成",
  "misfire_grace_time": 90,
  "log": {
    "level": "INFO",
    "output": ["stdout", "stderr", "file"],
    "module": false
  },
  "command_start": [],
  "sv": {}
}
```

## 5.5 配置修改的安全姿势

```sh
# 改前先备份
cp data/config.json data/config.json.bak.$(date +%s)

# 用 jq 安全改
jq '.PORT = "9527"' data/config.json > data/config.json.new
mv data/config.json.new data/config.json

# 改完用 python 验证 JSON 合法性
python -c "import json; json.load(open('data/config.json'))"
```

> Core 启动时会用 `update_config()` 给缺失字段补默认值，但**坏 JSON 直接抛
> `JSONDecodeError` 启动失败**，所以改完一定先验证。
