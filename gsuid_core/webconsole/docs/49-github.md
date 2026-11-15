# 49. GitHub 连接 API - /api/github

## 概述

GitHub 连接 API 让插件商店可以绑定部署者自己的 GitHub 账号（OAuth Device Flow，无需填 token），
并在商店卡片上显示各插件仓库的 star 数、直接对仓库 star / 取消 star。

**核心能力**：
- 查询当前 GitHub 连接状态
- Device Flow 授权（发起设备码 → 前端轮询授权结果）
- 断开连接
- 批量查询仓库 star 数与「当前账号是否已 star」
- star / 取消 star 指定仓库

**技术特点**：
- 一个 Core 只绑定一个 GitHub 账号；`access_token` 用 Fernet 加密后写入 `data/webconsole_github.json`
  （密钥派生自 `REGISTER_CODE`，改注册码后旧凭据解不开，自动视为未连接）
- `device_code` 只保存在进程内存（含过期时间与轮询间隔），不落盘
- 授权 scope 为 `public_repo`；授权响应缺少该 scope 时照常连接，但 `can_star=false`，star 操作被拒绝
- `POST /api/github/stars` 已连接时走 GraphQL（50 个仓库一批，同时查 `viewerHasStarred`）；
  未连接时走匿名 REST（并发 5，遇到 403/429 限流或连续网络失败即短路，已缓存的过期 star 数会保留，其余返回 null）
- star 数缓存 12 小时（内存 + 落盘 `data/webconsole_github_stars.json`；key 为小写 `owner/repo`，只缓存成功取到的 star 数）
- 任意请求收到 GitHub 401（用户已撤销授权）时自动清除本地凭据并按未连接处理

**认证方式**：所有 API 均需 `Authorization: Bearer <token>`；
`device/start`、`device/poll`、`disconnect`、`star`（PUT/DELETE）额外要求管理员权限。

---

## 49.1 获取连接状态

```
GET /api/github/status
```

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "connected": true,
        "login": "octocat",
        "avatar_url": "https://avatars.githubusercontent.com/u/1?v=4",
        "can_star": true
    }
}
```

| 字段 | 说明 |
|------|------|
| connected | 是否已绑定 GitHub 账号 |
| login / avatar_url | 已绑定账号的用户名与头像；未连接时为空字符串 |
| can_star | 授权是否含 `public_repo` scope；false 时前端应禁用 star 按钮并提示重新连接 |

## 49.2 发起 Device Flow 授权（管理员）

```
POST /api/github/device/start
```

后端向 `https://github.com/login/device/code` 申请设备码（`scope=public_repo`），
`device_code` 仅存后端内存，返回供用户操作的信息。

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "user_code": "XXXX-XXXX",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": 5
    }
}
```

## 49.3 轮询授权结果（管理员）

```
POST /api/github/device/poll
```

后端代为请求 `login/oauth/access_token` 一次，把 GitHub 状态映射为 `state` 返回；
前端按返回的 `interval` 间隔轮询。

| state | 含义 |
|-------|------|
| pending | 等待用户授权，继续按 interval 轮询 |
| slow_down | GitHub 要求降速，interval 已增大，按新值轮询 |
| expired | 设备码过期 / 不存在 / 已失效，需重新 start |
| denied | 用户拒绝授权 |
| connected | 授权完成，凭据已加密保存；附带 login / avatar_url / can_star |

```json
{ "status": 0, "msg": "ok", "data": { "state": "connected", "login": "octocat", "avatar_url": "...", "can_star": true } }
```

## 49.4 断开连接（管理员）

```
POST /api/github/disconnect
```

删除本地凭据与内存状态。Core 没有 client secret 无法远程吊销授权，
`msg` 会提示用户到 GitHub 设置的 Applications 页手动撤销。

## 49.5 批量查询 star 数

```
POST /api/github/stars
Content-Type: application/json

{ "repos": ["Genshin-bots/gsuid_core", "KimigaiiWuyi/GenshinUID"] }
```

- 元素为 `owner/repo` 格式；非法项被过滤、`.git` 后缀被去除、大小写不敏感去重，最多处理 100 个
- `count` / `starred` 为 `null` 表示仓库不存在、未连接或限流未取到

**响应**：

```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "connected": false,
        "stars": {
            "Genshin-bots/gsuid_core": { "count": 321, "starred": null },
            "KimigaiiWuyi/GenshinUID": { "count": 1500, "starred": null }
        }
    }
}
```

## 49.6 star / 取消 star（管理员，需已连接且 can_star=true）

```
PUT    /api/github/star/{owner}/{repo}
DELETE /api/github/star/{owner}/{repo}
```

成功返回 `{ "starred": true | false }`；若该仓库在缓存中有未过期的 star 数，会同步更新缓存计数。
GitHub 返回 403/404 时其 `message` 会透传到 `msg` 供前端回显。

### 错误处理建议

| 场景 | 建议处理方式 |
|------|-------------|
| `can_star=false` | 禁用 star 按钮，提示重新连接（旧 GitHub App 授权无 public_repo scope） |
| 401 / 授权失效 | 后端已自动清除凭据；提示重新连接 |
| stars 接口部分 null | 按「无数据」展示，不当作失败 |
| device/poll 返回 expired / denied | 提示用户并允许重新发起 start |
