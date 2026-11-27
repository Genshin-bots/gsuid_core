# 28. Git 版本管理 API - /api/git-update

## 概述

Git 版本管理 API 提供对所有插件（含 core 本体）的 git commit 历史查看、版本回退、强制更新和批量更新功能。用于前端实现版本管理页面，允许用户浏览历史更新记录并回退到任意版本。

**核心能力**：
- 获取所有插件的 git 状态信息（当前 commit、分支等）
- 获取单个插件的远程 commit 列表（历史更新记录）
- 获取单个插件的本地 commit 历史
- 回退到指定 commit（`git reset --hard`）
- 强制更新（`git reset --hard` + `git pull`）
- 一键更新全部插件到最新版本

**技术特点**：
- 所有 git 操作均通过 `asyncio.create_subprocess_shell` 异步执行，**不会阻塞事件循环**
- 不依赖 gitpython 进行核心操作，避免阻塞问题

**认证方式**：所有 API 均需通过 `Authorization: Bearer <token>` Header 携带访问令牌。

---

## 28.1 获取所有插件的 Git 状态

获取所有插件（含 core 本体）的 git 状态信息，包括当前 commit、分支等。这是前端页面的**主数据接口**，页面加载时应首先调用此接口。

```
GET /api/git-update/status?fresh=false
```

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `fresh` | `boolean` | `false` | 是否强制现扫磁盘与 commit（默认复用元信息中心内存快照） |

> 仅返回 `gsuid_core` 本体与具备 `origin` 远端的有效 Git 插件；非 Git 插件与无远端本地仓自动排除。
> 每条记录额外附带向后兼容字段：`remote_url`、`mirror`、`source_kind`、`upstream_host`、`upstream_url`。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "name": "gsuid_core",
            "path": "/path/to/gsuid_core",
            "branch": "main",
            "is_git_repo": true,
            "current_commit": {
                "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                "short_hash": "a1b2c3d",
                "author": "Developer",
                "date": "2024-01-15 10:30:00 +0800",
                "message": "feat: 添加新功能"
            }
        },
        {
            "name": "GenshinUID",
            "path": "/path/to/plugins/GenshinUID",
            "branch": "main",
            "is_git_repo": true,
            "current_commit": {
                "hash": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
                "short_hash": "b2c3d4e",
                "author": "Developer",
                "date": "2024-01-14 15:20:00 +0800",
                "message": "fix: 修复已知问题"
            }
        }
    ]
}
```

**字段说明**：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `data[].name` | `string` | 插件名称。core 本体固定为 `"gsuid_core"` |
| `data[].path` | `string` | 插件目录的绝对路径 |
| `data[].branch` | `string` | 当前分支名称 |
| `data[].is_git_repo` | `boolean` | 是否为有效 git 仓库 |
| `data[].current_commit` | `object` | 当前 HEAD 的 commit 信息 |
| `data[].current_commit.hash` | `string` | 完整 commit hash |
| `data[].current_commit.short_hash` | `string` | 短 commit hash（前 7 位） |
| `data[].current_commit.author` | `string` | 提交作者 |
| `data[].current_commit.date` | `string` | 提交日期 |
| `data[].current_commit.message` | `string` | 提交信息 |

---

## 28.2 获取单个插件的 Git 状态

获取指定插件的 git 状态信息。

```
GET /api/git-update/status/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "name": "gsuid_core",
        "path": "/path/to/gsuid_core",
        "branch": "main",
        "is_git_repo": true,
        "current_commit": {
            "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            "short_hash": "a1b2c3d",
            "author": "Developer",
            "date": "2024-01-15 10:30:00 +0800",
            "message": "feat: 添加新功能"
        }
    }
}
```

**错误响应**（插件不存在）：
```json
{
    "status": 1,
    "msg": "插件 NonExistent 不存在",
    "data": null
}
```

**错误响应**（非 git 仓库）：
```json
{
    "status": 1,
    "msg": "插件 SomePlugin 不是有效的 git 仓库",
    "data": null
}
```

---

## 28.3 获取远程 Commit 列表

获取插件的远程 commit 列表（历史更新记录）。会先执行 `git fetch` 获取最新远程信息，然后返回 `origin/{branch}` 的 commit 历史。

```
GET /api/git-update/commits/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**查询参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `max_count` | `int` | ❌ | `50` | 最大返回的 commit 数量 |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "plugin_name": "gsuid_core",
        "branch": "main",
        "current_hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "commits": [
            {
                "hash": "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
                "short_hash": "f6a1b2c",
                "author": "Developer",
                "date": "2024-01-16 09:00:00 +0800",
                "message": "feat: 最新功能"
            },
            {
                "hash": "e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
                "short_hash": "e5f6a1b",
                "author": "Developer",
                "date": "2024-01-15 10:30:00 +0800",
                "message": "feat: 添加新功能"
            },
            {
                "hash": "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
                "short_hash": "d4e5f6a",
                "author": "Developer",
                "date": "2024-01-14 15:20:00 +0800",
                "message": "fix: 修复已知问题"
            }
        ]
    }
}
```

**字段说明**：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `data.plugin_name` | `string` | 插件名称 |
| `data.branch` | `string` | 当前分支名称 |
| `data.current_hash` | `string` | 当前本地 HEAD 的 commit hash，用于前端标记"当前版本" |
| `data.commits` | `array` | 远程 commit 列表，按时间倒序排列 |
| `data.commits[].hash` | `string` | 完整 commit hash |
| `data.commits[].short_hash` | `string` | 短 commit hash（前 7 位） |
| `data.commits[].author` | `string` | 提交作者 |
| `data.commits[].date` | `string` | 提交日期 |
| `data.commits[].message` | `string` | 提交信息 |

**前端使用建议**：
- 通过比较 `current_hash` 与每个 commit 的 `hash`，可以标记当前所在的版本
- 可以用 `short_hash` 在 UI 中简洁展示
- `message` 字段用于展示更新日志

---

## 28.4 获取本地 Commit 历史

获取插件的本地 commit 历史（`git log`）。

```
GET /api/git-update/local-commits/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**查询参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `max_count` | `int` | ❌ | `50` | 最大返回的 commit 数量 |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "plugin_name": "gsuid_core",
        "branch": "main",
        "commits": [
            {
                "hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                "short_hash": "a1b2c3d",
                "author": "Developer",
                "date": "2024-01-15 10:30:00 +0800",
                "message": "feat: 添加新功能"
            }
        ]
    }
}
```

**字段说明**：与 28.3 相同，但 `data.commits` 是本地 commit 历史。

---

## 28.5 回退到指定 Commit

将插件回退到指定的 commit 版本。执行 `git reset --hard {commit_hash}`。

> ⚠️ **注意**：此操作会丢弃当前版本之后的所有本地修改。如需回到最新版本，请使用"强制更新"接口。

```
POST /api/git-update/checkout/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**请求体**：
```json
{
    "commit_hash": "a1b2c3d"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `commit_hash` | `string` | ✅ | 目标 commit hash，支持短 hash（至少 4 位） |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "已切换到 commit: a1b2c3d",
    "data": {
        "success": true,
        "message": "已切换到 commit: a1b2c3d"
    }
}
```

**错误响应**（无效 hash）：
```json
{
    "status": 1,
    "msg": "无效的 commit hash: xyz",
    "data": {
        "success": false,
        "message": "无效的 commit hash: xyz"
    }
}
```

**错误响应**（插件不存在）：
```json
{
    "status": 1,
    "msg": "插件 NonExistent 不存在",
    "data": null
}
```

---

## 28.6 强制更新

强制更新插件到最新版本。执行 `git reset --hard origin/{branch}` 然后 `git pull`。

适用于本地有冲突或修改时的强制更新场景，会**丢弃所有本地修改**。

```
POST /api/git-update/force-update/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**请求参数**：无

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "强制更新成功，当前版本: f6a1b2c",
    "data": {
        "success": true,
        "message": "强制更新成功，当前版本: f6a1b2c",
        "current_commit": {
            "hash": "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
            "short_hash": "f6a1b2c",
            "author": "Developer",
            "date": "2024-01-16 09:00:00 +0800",
            "message": "feat: 最新功能"
        }
    }
}
```

**错误响应**（reset --hard 失败）：
```json
{
    "status": 1,
    "msg": "git reset --hard 失败: ...",
    "data": {
        "success": false,
        "message": "git reset --hard 失败: ...",
        "current_commit": null
    }
}
```

**错误响应**（git fetch 失败，如网络问题）：
```json
{
    "status": 1,
    "msg": "git fetch 失败: Failed to connect to github.com port 443 after 21059 ms: Could not connect to server",
    "data": {
        "success": false,
        "message": "git fetch 失败: Failed to connect to github.com port 443 after 21059 ms: Could not connect to server",
        "current_commit": null
    }
}
```

**错误响应**（git pull 失败）：
```json
{
    "status": 1,
    "msg": "git pull 失败: ...",
    "data": {
        "success": false,
        "message": "git pull 失败: ...",
        "current_commit": null
    }
}
```

**错误响应**（插件不存在）：
```json
{
    "status": 1,
    "msg": "插件 NonExistent 不存在",
    "data": null
}
```

> 💡 **提示**：如果普通更新失败，错误信息中可能包含以下内容：
> - 网络错误（如 "Failed to connect to github.com port 443"）→ 建议检查网络或切换镜像源
> - 认证错误（如 "Authentication failed"）→ 建议检查 git 凭证配置
> - 本地冲突（如 "Your local changes to the following files would be overwritten"）→ 使用强制更新会丢失本地修改

---

## 28.7 普通更新单个插件

更新单个插件到最新版本。执行 `git fetch` + `git pull`。

适用于本地无冲突的正常更新场景。如果本地有修改，`git pull` 可能会失败。

```http
POST /api/git-update/update/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**请求参数**：无

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "更新成功，当前版本: f6a1b2c",
    "data": {
        "success": true,
        "message": "更新成功，当前版本: f6a1b2c",
        "current_commit": {
            "hash": "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
            "short_hash": "f6a1b2c",
            "author": "Developer",
            "date": "2024-01-16 09:00:00 +0800",
            "message": "feat: 最新功能"
        }
    }
}
```

**错误响应**（git fetch 失败，如网络问题）：
```json
{
    "status": 1,
    "msg": "git fetch 失败: Failed to connect to github.com port 443 after 21059 ms: Could not connect to server",
    "data": {
        "success": false,
        "message": "git fetch 失败: Failed to connect to github.com port 443 after 21059 ms: Could not connect to server",
        "current_commit": null
    }
}
```

**错误响应**（git pull 失败，如本地有冲突）：
```json
{
    "status": 1,
    "msg": "git pull 失败: error: Your local changes to the following files would be overwritten by merge...",
    "data": {
        "success": false,
        "message": "git pull 失败: error: Your local changes to the following files would be overwritten by merge...",
        "current_commit": null
    }
}
```

**错误响应**（插件不存在）：
```json
{
    "status": 1,
    "msg": "插件 NonExistent 不存在",
    "data": null
}
```

---

## 28.8 一键更新全部插件

一次性更新全部插件（含 core 本体）到最新版本。对每个插件执行普通更新（`git fetch` + `git pull`），返回每个插件的更新结果。

> ⚠️ **注意**：如果某个插件本地有修改导致 `git pull` 失败，该插件会返回失败，但不会影响其他插件。如需强制更新有冲突的插件，请使用"强制更新"接口。

> 💡 **提示**：如果更新失败，错误信息中可能包含以下内容：
> - 网络错误（如 "Failed to connect to github.com port 443"）→ 建议检查网络或切换镜像源
> - 本地冲突（如 "Your local changes to the following files would be overwritten"）→ 建议使用强制更新（但会丢失本地修改）
> - 认证错误（如 "Authentication failed"）→ 建议检查 git 凭证配置

```
POST /api/git-update/update-all
```

**请求参数**：无

**响应**（全部成功）：
```json
{
    "status": 0,
    "msg": "全部更新完成，共 3 个成功",
    "data": {
        "total": 3,
        "success_count": 3,
        "fail_count": 0,
        "results": [
            {
                "name": "gsuid_core",
                "success": true,
                "message": "更新成功，当前版本: f6a1b2c",
                "current_commit": {
                    "hash": "f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1",
                    "short_hash": "f6a1b2c",
                    "author": "Developer",
                    "date": "2024-01-16 09:00:00 +0800",
                    "message": "feat: 最新功能"
                }
            },
            {
                "name": "GenshinUID",
                "success": true,
                "message": "更新成功，当前版本: b2c3d4e",
                "current_commit": {
                    "hash": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
                    "short_hash": "b2c3d4e",
                    "author": "Developer",
                    "date": "2024-01-14 15:20:00 +0800",
                    "message": "fix: 修复已知问题"
                }
            },
            {
                "name": "SomePlugin",
                "success": true,
                "message": "更新成功，当前版本: c3d4e5f",
                "current_commit": {
                    "hash": "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                    "short_hash": "c3d4e5f",
                    "author": "Developer",
                    "date": "2024-01-13 12:00:00 +0800",
                    "message": "chore: 更新依赖"
                }
            }
        ]
    }
}
```

**响应**（存在失败）：
```json
{
    "status": 1,
    "msg": "更新完成，2 个成功，1 个失败",
    "data": {
        "total": 3,
        "success_count": 2,
        "fail_count": 1,
        "results": [
            {
                "name": "gsuid_core",
                "success": true,
                "message": "更新成功，当前版本: f6a1b2c",
                "current_commit": { "..." : "..." }
            },
            {
                "name": "GenshinUID",
                "success": true,
                "message": "更新成功，当前版本: b2c3d4e",
                "current_commit": { "..." : "..." }
            },
            {
                "name": "BrokenPlugin",
                "success": false,
                "message": "git pull 失败: ...",
                "current_commit": null
            }
        ]
    }
}
```

**响应**（无插件可更新）：
```json
{
    "status": 2,
    "msg": "没有可更新的插件",
    "data": {
        "total": 0,
        "success_count": 0,
        "fail_count": 0,
        "results": []
    }
}
```

**字段说明**：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `data.total` | `int` | 总插件数量 |
| `data.success_count` | `int` | 更新成功的插件数量 |
| `data.fail_count` | `int` | 更新失败的插件数量 |
| `data.results` | `array` | 每个插件的更新结果 |
| `data.results[].name` | `string` | 插件名称 |
| `data.results[].success` | `boolean` | 是否更新成功 |
| `data.results[].message` | `string` | 更新结果消息 |
| `data.results[].current_commit` | `object \| null` | 更新后的 commit 信息，失败时为 `null` |

---

## 前端集成指南

### 页面布局建议

```
┌──────────────────────────────────────────────────────────────────┐
│  Git 版本管理                                                      │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  插件选择: [ gsuid_core ▼ ]                                       │
│                                                                  │
│  当前状态: 分支 main | commit a1b2c3d | 2024-01-15 10:30          │
│                                                                  │
│  ┌─ 远程 Commit 列表（历史更新记录）──────────────────────────────┐ │
│  │  ● f6a1b2c  2024-01-16  feat: 最新功能           [回退到此版本] │ │
│  │  ● e5f6a1b  2024-01-15  feat: 添加新功能  ← 当前  [回退到此版本] │ │
│  │  ● d4e5f6a  2024-01-14  fix: 修复已知问题         [回退到此版本] │ │
│  │  ● c3d4e5f  2024-01-13  refactor: 重构代码        [回退到此版本] │ │
│  │  ...                                                           │ │
│  └──────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  [ 🔄 更新到最新版本 ]  [ ⚠️ 强制更新 ]  [ 📦 一键更新全部插件 ]        │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 推荐交互流程

#### 1. 页面加载

```
页面加载
  │
  ├─→ GET /api/git-update/status
  │     │
  │     └─→ 渲染插件选择器（下拉框）
  │           - 展示所有 is_git_repo=true 的插件
  │           - 默认选中 gsuid_core
  │
  ├─→ 用户选择插件（或默认选中）
  │     │
  │     └─→ GET /api/git-update/commits/{plugin_name}
  │           │
  │           ├─→ 渲染当前状态信息（分支、当前 commit）
  │           │
  │           └─→ 渲染 commit 列表
  │                 - 标记 current_hash 对应的行为"当前版本"
  │                 - 每行显示 short_hash、date、message
  │                 - 每行提供"回退到此版本"按钮
  │
  └─→ 页面就绪
```

#### 2. 回退到指定版本

```
用户点击某行的"回退到此版本"按钮
  │
  ├─→ 弹出确认对话框
  │     "确认将 xxx 回退到 commit a1b2c3d (feat: xxx)？"
  │     "注意：回退后当前版本之后的本地修改将被丢弃"
  │
  ├─→ 用户确认
  │     │
  │     └─→ POST /api/git-update/checkout/{plugin_name}
  │           {commit_hash: "a1b2c3d"}
  │           │
  │           ├─→ 成功
  │           │     - 显示成功提示
  │           │     - 刷新 commit 列表
  │           │       GET /api/git-update/commits/{plugin_name}
  │           │
  │           └─→ 失败
  │                 - 显示错误提示
  │
  └─→ 用户取消 → 无操作
```

#### 3. 强制更新

```
用户点击"强制更新到最新版本"按钮
  │
  ├─→ 弹出确认对话框
  │     "确认强制更新 xxx？"
  │     "此操作将丢弃所有本地修改，不可恢复！"
  │
  ├─→ 用户确认
  │     │
  │     └─→ POST /api/git-update/force-update/{plugin_name}
  │           │
  │           ├─→ 成功
  │           │     - 显示成功提示，包含最新 commit 信息
  │           │     - 刷新 commit 列表
  │           │       GET /api/git-update/commits/{plugin_name}
  │           │
  │           └─→ 失败
  │                 - 显示错误提示
  │
  └─→ 用户取消 → 无操作
```

#### 4. 普通更新

```
用户点击"更新到最新版本"按钮
  │
  ├─→ POST /api/git-update/update/{plugin_name}
  │     │
  │     ├─→ 成功
  │     │     - 显示成功提示，包含最新 commit 信息
  │     │     - 刷新 commit 列表
  │     │       GET /api/git-update/commits/{plugin_name}
  │     │
  │     └─→ 失败
  │           - 显示错误提示
  │           - 根据错误信息建议：
  │             · 网络错误 → 建议检查网络或切换镜像源
  │             · 本地冲突 → 建议使用强制更新（但会丢失本地修改）
  │             · 认证错误 → 建议检查 git 凭证配置
```

#### 5. 一键更新全部插件

```
用户点击"一键更新全部插件"按钮
  │
  ├─→ POST /api/git-update/update-all
  │     │
  │     ├─→ 成功（status=0）
  │     │     - 显示成功提示 "全部更新完成，共 N 个成功"
  │     │     - 刷新插件状态列表
  │     │       GET /api/git-update/status
  │     │
  │     ├─→ 部分失败（status=1）
  │     │     - 显示结果摘要 "N 个成功，M 个失败"
  │     │     - 展示失败插件列表及错误原因
  │     │     - 对每个失败的插件提供"重试"或"强制更新"选项
  │     │     - 刷新插件状态列表
  │     │       GET /api/git-update/status
  │     │
  │     └─→ 无插件（status=2）
  │           - 显示提示 "没有可更新的插件"
```
  │
  ├─→ 用户确认
  │     │
  │     └─→ POST /api/git-update/update-all
  │           │
  │           ├─→ 成功（status=0）
  │           │     - 显示成功提示 "全部更新完成，共 N 个成功"
  │           │     - 刷新插件状态列表
  │           │       GET /api/git-update/status
  │           │
  │           ├─→ 部分失败（status=1）
  │           │     - 显示结果摘要 "N 个成功，M 个失败"
  │           │     - 展示失败插件列表及错误原因
  │           │     - 刷新插件状态列表
  │           │       GET /api/git-update/status
  │           │
  │           └─→ 无插件（status=2）
  │                 - 显示提示 "没有可更新的插件"
  │
  └─→ 用户取消 → 无操作
```

### 错误处理建议

| 场景 | 建议处理方式 |
|------|-------------|
| 网络请求失败 | 显示 toast 错误提示，保留当前页面状态 |
| 插件不是 git 仓库 | 在插件选择器中禁用该选项，或显示"非 git 仓库"提示 |
| 插件不存在 | 显示 toast 错误提示 "插件 xxx 不存在" |
| 无效的 commit hash | 显示 toast 错误提示，提示用户检查 hash 值 |
| git fetch 失败（网络问题） | 显示错误提示，建议检查网络连接或切换镜像源 |
| git pull 失败（本地冲突） | 提示用户可以选择"强制更新"（但会丢失本地修改），或者先提交/stash 本地修改 |
| git pull 失败（Already up to date） | 显示提示 "已是最新版本" |
| 认证失败 | 提示用户检查 git 凭证配置 |
| 回退后需要恢复 | 提示用户使用"强制更新"回到最新版本 |

### 批量更新失败时的用户引导

当用户点击"一键更新全部插件"后，如果部分插件更新失败（如网络问题或本地冲突），前端应该：

1. **显示汇总信息**：列出成功和失败的插件数量
2. **对失败插件提供操作选项**：
   - 对于网络问题导致的失败，提供"切换镜像源"链接
   - 对于本地冲突导致的失败，提供"强制更新"选项（需用户确认会丢失本地修改）
   - 对于认证问题，提供"检查 git 凭证"提示
3. **允许重试**：对失败的插件提供单独重试按钮

### 与 Git 镜像源管理的关系

如果远程仓库访问速度慢，建议先通过 [Git 镜像源管理 API](./25-git-mirror.md) 切换到国内镜像源，再进行版本管理操作，以获得更好的体验。
