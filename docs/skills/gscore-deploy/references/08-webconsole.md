# 八、WebConsole 网页控制台

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[七、WebSocket 安全](./07-security-ws.md) · **下一章**：[九、插件管理体系](./09-plugins.md)

WebConsole 是 GsCore 自带的**网页管理后台**，从 [`commit f903e3`](https://github.com/Genshin-bots/gsuid_core/commit/f903e3d0569499e1a8393d37b9605d35480233ed) 之后默认随 Core 启动，无需手动开启。

## 8.1 启动自动装配

[`gsuid_core/app_life.py`](../../../gsuid_core/app_life.py) 的 `lifespan`：

1. 启动前钩子（数据库迁移、global_val 加载）
2. `setup_frontend_b()`（后台异步，缺 dist 时从 CDN 拉）
3. `start_scheduler()`（定时任务）
4. `clean_trace_collector()`（后台异步；`clean_log()` 已废除，日志缓冲改为 append 时按
   条数/字符数上限淘汰，不再周期性清空）

[`gsuid_core/webconsole/app_app.py`](../../../gsuid_core/webconsole/app_app.py) 把
FastAPI `app` 暴露给所有 `webconsole/*.py` 路由模块。WebConsole 路由分类见
[十五、数据目录 §15.5](./15-data-layout.md#155-webconsole-路由模块清单)。

## 8.2 地址 & 注册码

- **默认地址**：`http://localhost:8765/app`
- **外网访问**：Core 改 `HOST=0.0.0.0` + 反代 / 端口映射
- **首次进入**：用 `config.json` 的 `REGISTER_CODE` 注册（32 位 hex）

```
打开浏览器 → http://HOST:PORT/app
↓
输入 REGISTER_CODE → 设置用户名 / 邮箱 / 密码 → 注册管理员
↓
登录
```

> ⚠️ 只能注册**一个**管理员账号。注册后密码 / 邮箱走 WebConsole「账号设置」
> 改；忘密码删 `data/GsData.db` 的 `web_user` 表 + 改 `REGISTER_CODE` 或直接
> 重置 `data/` 目录重建（玩家数据会丢，DB 备份可救）。

## 8.3 功能矩阵

| 页签 | 内容 | 是否热更 |
|------|------|----------|
| 仪表盘 | 启动耗时 / 插件数 / 服务数 / 触发器数 / AI 统计 | 实时 |
| 任务调度 | 监控 / 暂停 / 手动跑定时任务 | 重启失效 |
| 主题系统 | 背景 / 图标 / 纯色 / 毛玻璃 / 风格 | 实时 |
| 数据统计 | 调用量 / 用户活跃 / 错误率 | 实时 |
| 插件功能配置 | 插件自己注册的功能开关 | 多数可热更 |
| 插件参数配置 | `plugins_configs/<plugin>.json` | 多数可热更 |
| 核心配置 | `config.json` + `core_config.json` | **多数需重启** |
| 数据表管理 | 浏览 / 编辑 `GsData.db` 的表 | 实时 |
| 插件管理 | 安装 / 更新 / 卸载 / 启动顺序 / 日志 | 实时 |
| 历史日志 | 过滤历史日志（可按天 / 等级 / 关键字） | 实时 |
| 实时日志 | tail 当前进程日志 | 实时 |
| 备份管理 | 选路径 / 立即备份 / 定时备份 | 实时 |

## 8.4 加密握手协议（应用层 HTTPS）

**为什么需要**：Core 只暴露 HTTP（无内置 TLS），登录 / 注册 / 改密报文里的密码
会以**明文**出现在传输层，存在同网段嗅探 / ISP 窥探 / 抓包回放风险。Core 提供
**应用层加密**通道（ECDH + HKDF + AES-256-GCM），不依赖 HTTPS 证书。

> ⚠️ **2026-06-15 起为强制、无开关、无明文兼容**：所有认证报文必须是加密形态
> （`enc=true` + 握手字段），明文 / `enc!=true` 一律被 `AuthCryptoError` 拒绝；不再有
> `REQUIRE_ENCRYPTED_AUTH` 配置。**部署 / 升级时务必确保前端 bundle 已落地加密实现**
> （先取 `/api/auth/pubkey` 再提交加密报文），否则登录 / 注册 / 改密全部会被拒。

源码：[`gsuid_core/webconsole/auth_crypto.py`](../../../gsuid_core/webconsole/auth_crypto.py)
+ [协议文档：`docs/WEBCONSOLE_AUTH_ENCRYPTION.md`](../../../WEBCONSOLE_AUTH_ENCRYPTION.md)（如果有）。

### 8.4.1 协议概览

```
前端                                              后端 (Core)
  │
  ├─ GET /api/auth/pubkey ─────────────────────→ 返回 key_id + 公钥 + fingerprint
  │
  ├─ 临时生成 X25519 keypair
  ├─ 与服务端公钥做 ECDH → shared_key
  ├─ HKDF-SHA256 派生 32B 对称密钥
  ├─ AES-256-GCM 加密 {email, password, ts} → {iv, ct}
  │
  ├─ POST /api/auth/register ─────────────────→ {enc:true, key_id, client_pub, iv, ct}
  │                                              │
  │                                              ├─ 用 key_id 取服务端私钥
  │                                              ├─ ECDH + HKDF 派生
  │                                              ├─ AES-GCM 解密
  │                                              ├─ 校验 ts 在 ±120s 内
  │                                              └─ 业务校验（邮箱 / 密码 / 注册码）
  │
  └─ 后续登录 / 改密同样走加密
```

**关键参数**（[`auth_crypto.py`](../../../gsuid_core/webconsole/auth_crypto.py)）：

| 参数 | 值 | 含义 |
|------|----|------|
| `PROTOCOL_ID` | `"gsuid-webconsole-auth/v1"` | HKDF info，前端必须一致 |
| `TS_TOLERANCE_SECONDS` | `120` | 时间戳容忍窗口（防重放） |
| `_DERIVED_KEY_LEN` | `32` | AES-256 派生密钥长度 |
| `KEY_ROTATION_INTERVAL_HOURS` | `12` | 服务端密钥每 12h 自动轮换 |

### 8.4.2 安全性

| 威胁 | 防护 |
|------|------|
| 被动嗅探 | 密码永不出现在明文流量 |
| 重放攻击 | `ts` 时间戳 ±120s 窗口 |
| 前向保密 | 每次握手用前端临时 keypair |
| MITM 篡改 | AES-GCM 认证失败直接 reject + IP 限流 |

**边界**：

- **不防主动 MITM 篡改前端 bundle**（与 HTTPS 自签名首次访问的 TOFU 局限同等）。
- 想彻底防 MITM → 上 HTTPS（见 [7.2.1 Nginx 反代示例](./07-security-ws.md#721-nginx-反代--tls-示例)）。

### 8.4.3 公钥指纹自检

`/api/auth/pubkey` 返回里包含 `fingerprint`（SHA-256 前 16 字符）。首次访问
WebConsole 时人工对比 Core 启动日志：

```
🔒️ [网页控制台] 认证加密密钥已生成 key_id=abc12345 pubkey_fingerprint=...
```

如果对不上 → 可能在被 MITM，**不要输入密码**。

## 8.5 限流（认证接口）

[`AuthRateLimiter`](../../../gsuid_core/security_manager.py)：

| 常量 | 值 | 含义 |
|------|----|------|
| `WINDOW` | `60` | 滑动窗口 60s |
| `MAX_ATTEMPTS` | `10` | 窗口内最多 10 次 |
| `MAX_FAILURES` | `5` | 连续失败 5 次封禁 |
| `BAN_DURATION` | `900` | 封禁 900s |

**两层防护**：

1. **解密前置限流**（`authdec:<ip>` key）：畸形 / 重放报文与认证失败同等对待，防 DoS。
2. **业务限流**（`login:` / `register:` / `password:` key）：登录 / 注册 / 改密分别
   计数。

封禁触发后前端提示：

```
操作过于频繁，请在 895 秒后重试
```

> 状态在内存，重启清空。

## 8.6 主题持久化

主题配置存在 `data/theme_config.json`，WebConsole 改完自动写盘。**不要手动
改**，否则会被覆盖。

## 8.7 dist 自举

首次启动如果 `data/dist/` 不存在，`setup_frontend_b()` 会从 CDN 拉前端 bundle
到 `data/dist/`。**慢的话**：

```sh
# 手动从 GitHub release 下载对应版本
wget -O data/dist.zip https://github.com/Genshin-bots/gsuid_core/releases/latest/download/dist.zip
unzip data/dist.zip -d data/
```

## 8.8 故障排查

| 现象 | 原因 |
|------|------|
| 打开 `/app` 是空白 / 404 | `data/dist/` 缺前端包 / 拉取失败 |
| 登录提示「请求无效，请刷新页面后重试」 | 前端 bundle 过旧 / 时钟漂移 / 被 MITM |
| 登录提示「操作过于频繁」 | 触发限流（60s 内超 10 次或 5 连败） |
| 忘了注册码 | `cat data/config.json | jq .REGISTER_CODE` |
| 忘了密码 | 删 `web_user` 表或重建 `data/` |
| 想换端口 | 改 `config.json` 的 `PORT` 重启 |
| 想禁止公网访问 | `HOST=localhost` / 反代层做 IP 白名单 |

更多：[十六、故障排查清单](./16-troubleshooting.md)
