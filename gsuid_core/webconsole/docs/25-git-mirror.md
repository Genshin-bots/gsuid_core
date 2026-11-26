# 25. Git 镜像源管理 API - /api/git-mirror

## 概述

Git 镜像源管理 API 提供对所有插件 git remote URL 的查看和修改能力。用于解决国内访问 GitHub 速度慢的问题，支持将所有插件的 git remote 地址一键切换到镜像源、代理或 SSH，也支持恢复为 GitHub 原始地址。

**核心能力**：
- 查看当前配置的镜像源及所有可用镜像源选项
- 查看所有插件（含 core 本体）的 git remote URL 和当前使用的镜像状态
- **一键批量替换**所有已安装插件的 git remote URL 到指定镜像源/代理/SSH
- **单独修改**某个插件的 git remote URL
- 设置镜像源后，**后续新安装的插件**也会自动使用该镜像源

**支持三种模式**：

| 模式 | 说明 | 示例 |
|------|------|------|
| **镜像模式** | 将 GitHub 地址替换为镜像站地址 | `https://gitcode.com/gscore-mirror/GenshinUID` |
| **代理前缀模式** | 在 GitHub 地址前添加代理前缀 | `https://ghproxy.mihomo.me/https://github.com/xxx/GenshinUID` |
| **SSH 模式** | 使用 SSH 协议连接 GitHub | `ssh://git@ssh.github.com:443/xxx/GenshinUID.git` |

**认证方式**：所有 API 均需通过 `Authorization: Bearer <token>` Header 携带访问令牌。

---

## 25.1 获取 Git 镜像信息

获取当前镜像配置、所有可用镜像源选项，以及每个插件的 git remote URL 和镜像状态。这是前端页面的**主数据接口**，页面加载时应首先调用此接口。

```
GET /api/git-mirror/info?fresh=false&probe=false
```

**查询参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `fresh` | `boolean` | `false` | 是否强制现扫磁盘刷新本地插件元信息 |
| `probe` | `boolean` | `false` | 是否并发探活官方镜像源（CNB / GitCode）可用性 |

> 仅返回 `gsuid_core` 本体与具备 `origin` 远端的 Git 插件；非 Git 插件与无 `origin` 本地仓自动过滤。

**新增响应字段**（向后兼容）：
- `data.current_mirror_mode`：`auto` / `proxy` / `mirror`
- `data.available_mirrors[].channel`：稳定通道标识（`github` / `gitcode` / `cnb` / `ghproxy` / `ssh`）
- `data.plugins[].source_kind`：`official` / `fork` / `third_party`
- `data.plugins[].upstream_url` / `upstream_host` / `upstream_provider` / `active_mirror_prefix` / `store_upstream_url`
- `data.plugins[].supported_mirrors`：允许切换的内置通道数组（原生非 GitHub 源为 `[]`；Fork/第三方不含 `cnb`/`gitcode`）
- `data.plugins[].mirror_probes`：探活结果数组 `[{channel, target_url, state, checked_at, expires_at, detail}]`
- `data.plugins[].mirror`：扩展支持 `custom_proxy`、`custom_mirror`、`native`

**按需探活接口**（管理员）：
```
POST /api/git-mirror/probe
Body: { "plugin_name": "GenshinUID" | null, "force": false }
```

**原子保存全局镜像配置接口**（管理员，不影响已安装插件，仅作用于后续新装插件）：
```
POST /api/git-mirror/config
Body: { "mirror_prefix": "https://my-proxy.example.com/", "mirror_type": "proxy" }
Response: { "status": 0, "msg": "ok", "data": { "current_mirror": "...", "current_mirror_mode": "proxy" } }
```

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "current_mirror": "https://gitcode.com/gscore-mirror/",
        "available_mirrors": [
            {"label": "GitHub (默认)", "value": "", "type": "default"},
            {"label": "GitCode 镜像", "value": "https://gitcode.com/gscore-mirror/", "type": "mirror"},
            {"label": "CNB 镜像", "value": "https://cnb.cool/gscore-mirror/", "type": "mirror"},
            {"label": "ghproxy 代理", "value": "https://ghproxy.mihomo.me/", "type": "proxy"},
            {"label": "GitHub SSH", "value": "ssh://", "type": "ssh"}
        ],
        "plugins": [
            {
                "name": "gsuid_core",
                "path": "/path/to/gsuid_core",
                "remote_url": "https://gitcode.com/gscore-mirror/gsuid_core",
                "is_git_repo": true,
                "mirror": "gitcode",
                "commit": "a1b2c3d"
            },
            {
                "name": "GenshinUID",
                "path": "/path/to/plugins/GenshinUID",
                "remote_url": "ssh://git@ssh.github.com:443/Genshin-bots/GenshinUID.git",
                "is_git_repo": true,
                "mirror": "ssh",
                "commit": "e5f6g7h"
            },
            {
                "name": "StarRailUID",
                "path": "/path/to/plugins/StarRailUID",
                "remote_url": "https://ghproxy.mihomo.me/https://github.com/xxx/StarRailUID",
                "is_git_repo": true,
                "mirror": "ghproxy",
                "commit": "i9j0k1l2"
            },
            {
                "name": "ZZZeroUID",
                "path": "/path/to/plugins/ZZZeroUID",
                "remote_url": "https://github.com/xxx/ZZZeroUID",
                "is_git_repo": true,
                "mirror": "github",
                "commit": "m3n4o5p6"
            },
            {
                "name": "SomePlugin",
                "path": "/path/to/plugins/SomePlugin",
                "remote_url": "",
                "is_git_repo": false,
                "mirror": "unknown",
                "commit": ""
            }
        ]
    }
}
```

**字段说明**：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `data.current_mirror` | `string` | 当前配置的镜像源前缀。空字符串 `""` 表示未配置（使用 GitHub 默认地址） |
| `data.available_mirrors` | `array` | 可用镜像源选项列表，可直接用于渲染下拉框/单选按钮组 |
| `data.available_mirrors[].label` | `string` | 镜像源显示名称，用于 UI 展示 |
| `data.available_mirrors[].value` | `string` | 镜像源前缀 URL，用于 API 调用时传参 |
| `data.available_mirrors[].type` | `string` | 类型标识：`"default"` / `"mirror"` / `"proxy"` / `"ssh"`，前端可用于分组展示 |
| `data.plugins` | `array` | 所有插件（含 core 本体）的 git 信息列表 |
| `data.plugins[].name` | `string` | 插件名称。core 本体固定为 `"gsuid_core"` |
| `data.plugins[].path` | `string` | 插件目录的绝对路径（仅供参考） |
| `data.plugins[].remote_url` | `string` | 当前 origin remote URL。非 git 仓库时为空字符串 |
| `data.plugins[].is_git_repo` | `boolean` | 是否为有效 git 仓库。`false` 表示该目录不是通过 git clone 安装的 |
| `data.plugins[].mirror` | `string` | 当前使用的镜像源标识，见下方状态枚举表 |

**`mirror` 字段枚举值**：

| 值 | 含义 | 建议 UI 展示 |
|----|------|-------------|
| `"gitcode"` | 使用 GitCode 镜像 | 🟢 绿色标签 "GitCode" |
| `"cnb"` | 使用 CNB 镜像 | 🟢 绿色标签 "CNB" |
| `"ghproxy"` | 使用 ghproxy 代理 | 🔵 蓝色标签 "ghproxy" |
| `"ssh"` | 使用 GitHub SSH | 🟣 紫色标签 "SSH" |
| `"github"` | 使用 GitHub 原始地址 | ⚪ 灰色标签 "GitHub" |
| `"unknown"` | 非 git 仓库或未知地址 | 🟡 黄色标签 "未知" |

---

## 25.2 批量设置所有插件的镜像源

将所有已安装插件（包括 core 本体）的 git remote URL **一键切换**到指定镜像源/代理/SSH。同时会更新配置文件中的 `GitMirror` 配置项，使后续新安装的插件也自动使用该镜像源。

```
POST /api/git-mirror/set-all
```

**请求体**：
```json
{
    "mirror_prefix": "https://gitcode.com/gscore-mirror/",
    "mirror_type": "auto"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mirror_prefix` | `string` | ✅ | 镜像源/反代前缀。空字符串 `""` 恢复 GitHub 默认，`"ssh://"` 切换 SSH；自定义地址须通过 SSRF 校验 |
| `mirror_type` | `string` | ❌ | `auto`（默认，内置源自动识别，自定义地址按 `proxy`）/ `default` / `proxy` / `mirror` / `ssh` |

> 切源前全通道强制预检可拉取性；Fork / 第三方 / 非 GitHub 原生源不适用时返回 `status: "skipped"`，计入 `summary.skipped_count`。仅当恢复默认、已知内置源无失败项，或至少一项切源成功时才更新全局 `GitMirror` / `GitMirrorMode`（见 `summary.config_updated`）。

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "results": [
            {
                "name": "gsuid_core",
                "success": true,
                "status": "switched",
                "message": "gsuid_core: https://github.com/Genshin-bots/gsuid_core -> https://gitcode.com/gscore-mirror/gsuid_core"
            },
            {
                "name": "GenshinUID",
                "success": true,
                "status": "skipped",
                "message": "GenshinUID: Fork 仓库 (user-b/GenshinUID) 不允许切换至扁平镜像源，请改用反代或 SSH"
            },
            {
                "name": "NewPlugin",
                "success": false,
                "status": "failed",
                "message": "NewPlugin: 预检未通过（目标仓库未同步或不存在 (HTTP 404)），已保留原源"
            }
        ],
        "summary": {
            "total": 3,
            "success_count": 1,
            "skipped_count": 1,
            "fail_count": 1,
            "config_updated": true
        }
    }
}
```

**字段说明**：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `data.results` | `array` | 每个含 `origin` 的 Git 插件处理结果 |
| `data.results[].name` | `string` | 插件名称 |
| `data.results[].success` | `boolean` | 是否成功（`switched` / `unchanged` / `skipped` 为 `true`，`failed` 为 `false`） |
| `data.results[].status` | `string` | `switched` / `unchanged` / `skipped` / `failed` |
| `data.results[].message` | `string` | 详细信息，包含原/目标 URL 或跳过/拦截原因 |
| `data.summary.total` | `int` | 处理的插件总数（含 core 本体） |
| `data.summary.success_count` | `int` | `switched` + `unchanged` 数量 |
| `data.summary.skipped_count` | `int` | 不适用并自动跳过的数量（如 Fork / 非 GitHub 原生源） |
| `data.summary.fail_count` | `int` | 预检未通过或写入失败的数量 |
| `data.summary.config_updated` | `boolean` | 本次是否同步更新了全局 `GitMirror` / `GitMirrorMode` 配置 |

**副作用**：
- 仅当 `summary.config_updated` 为 `true` 时，才会同步更新 `GitMirror` 与 `GitMirrorMode` 配置项
- 后续通过插件商店安装的新插件将按上述配置与归属规则使用镜像源/反代/SSH

---

## 25.3 设置单个插件的镜像源

将指定插件的 git remote URL 切换到指定镜像源/反代/SSH。适用于只想切换某个特定插件的场景。

```
POST /api/git-mirror/set-plugin/{plugin_name}
```

**路径参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `plugin_name` | `string` | 插件名称。`"gsuid_core"` 表示 core 本体。大小写不敏感 |

**请求体**：
```json
{
    "mirror_prefix": "ssh://",
    "mirror_type": "auto"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mirror_prefix` | `string` | ✅ | 镜像源/反代前缀。空字符串 `""` 恢复 GitHub 默认，`"ssh://"` 切换 SSH；自定义地址须通过 SSRF 校验 |
| `mirror_type` | `string` | ❌ | `auto`（默认）/ `default` / `proxy` / `mirror` / `ssh` |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "GenshinUID: https://github.com/xxx/GenshinUID -> ssh://git@ssh.github.com:443/xxx/GenshinUID.git",
    "data": {
        "name": "GenshinUID",
        "success": true,
        "message": "GenshinUID: https://github.com/xxx/GenshinUID -> ssh://git@ssh.github.com:443/xxx/GenshinUID.git"
    }
}
```

**响应**（已是目标地址，无需修改）：
```json
{
    "status": 0,
    "msg": "GenshinUID: 已是目标地址，无需修改",
    "data": {
        "name": "GenshinUID",
        "success": true,
        "message": "GenshinUID: 已是目标地址，无需修改"
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
    "msg": "SomePlugin: 非 git 仓库或无 origin remote",
    "data": {
        "name": "SomePlugin",
        "success": false,
        "message": "SomePlugin: 非 git 仓库或无 origin remote"
    }
}
```

**注意**：此接口**不会**更新配置文件中的 `GitMirror` 配置项，仅修改指定插件的 git remote URL。如需同时更新配置，请使用批量接口或通过核心配置 API 单独设置。

---

## 25.4 获取可用镜像源列表

获取所有可用的镜像源/代理/SSH 选项。此接口返回的数据与 25.1 中的 `available_mirrors` 字段相同，提供一个轻量级的独立访问方式。

```
GET /api/git-mirror/available
```

**请求参数**：无

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {"label": "GitHub (默认)", "value": "", "type": "default"},
        {"label": "GitCode 镜像", "value": "https://gitcode.com/gscore-mirror/", "type": "mirror"},
        {"label": "CNB 镜像", "value": "https://cnb.cool/gscore-mirror/", "type": "mirror"},
        {"label": "ghproxy 代理", "value": "https://ghproxy.mihomo.me/", "type": "proxy"},
        {"label": "GitHub SSH", "value": "ssh://", "type": "ssh"}
    ]
}
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `data[].label` | `string` | 镜像源显示名称，用于 UI 展示 |
| `data[].value` | `string` | 镜像源前缀 URL，用于 API 调用时传参。空字符串表示默认（GitHub） |
| `data[].type` | `string` | 类型标识：`"default"` / `"mirror"` / `"proxy"` / `"ssh"` |

---

## URL 转换规则详解

系统支持三种不同的 URL 转换模式，前端应了解其差异以便正确展示：

### 镜像模式（GitCode / CNB）

将 GitHub 地址中的仓库名提取出来，拼接到镜像前缀后面：

```
原始地址:  https://github.com/Genshin-bots/GenshinUID
镜像前缀:  https://gitcode.com/gscore-mirror/
转换结果:  https://gitcode.com/gscore-mirror/GenshinUID
```

```
原始地址:  https://github.com/Genshin-bots/gsuid_core
镜像前缀:  https://cnb.cool/gscore-mirror/
转换结果:  https://cnb.cool/gscore-mirror/gsuid_core
```

### 代理前缀模式（ghproxy）

在完整的 GitHub 地址前添加代理前缀：

```
原始地址:  https://github.com/Genshin-bots/GenshinUID
代理前缀:  https://ghproxy.mihomo.me/
转换结果:  https://ghproxy.mihomo.me/https://github.com/Genshin-bots/GenshinUID
```

### SSH 模式

将地址转换为 SSH 格式，使用 `ssh.github.com:443` 端口（适用于防火墙限制 22 端口的环境）：

```
原始地址:  https://github.com/Genshin-bots/GenshinUID
转换结果:  ssh://git@ssh.github.com:443/Genshin-bots/GenshinUID.git
```

```
原始地址:  https://gitcode.com/gscore-mirror/ArknightsUID
转换结果:  ssh://git@ssh.github.com:443/gscore-mirror/ArknightsUID.git
```

> **注意**：SSH 模式需要用户在服务器上配置好 GitHub SSH 密钥（`ssh-keygen` + 添加到 GitHub 账户），否则 `git clone`/`git pull` 会因认证失败而报错。

### 恢复为 GitHub

无论当前使用哪种模式，恢复时都会统一转换为 `https://github.com/gscore-mirror/{仓库名}` 格式。

---

## 前端集成指南

### 页面布局建议

```
┌──────────────────────────────────────────────────────────────┐
│  Git 镜像源管理                                                │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  当前镜像源: [ GitCode 镜像 ▼ ]         [ 一键应用到所有插件 ]  │
│                                                              │
│  ┌─ 镜像源 ────────────────────────────────────────────────┐ │
│  │  ○ GitHub (默认)                                         │ │
│  │  ● GitCode 镜像    https://gitcode.com/gscore-mirror/   │ │
│  │  ○ CNB 镜像        https://cnb.cool/gscore-mirror/      │ │
│  │  ○ ghproxy 代理    https://ghproxy.mihomo.me/           │ │
│  │  ○ GitHub SSH      ssh://git@ssh.github.com:443/        │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│  插件列表                                                     │
│  ┌──────────┬──────────────────────────┬────────┬────────┐  │
│  │ 插件名称  │ 当前 Remote URL           │ 镜像状态 │ 操作   │  │
│  ├──────────┼──────────────────────────┼────────┼────────┤  │
│  │gsuid_core│gitcode.com/.../gsuid_core│ GitCode│ [切换] │  │
│  │GenshinUID│ssh://git@ssh.github.com..│  SSH   │ [切换] │  │
│  │StarRail..│ghproxy.mihomo.me/https://│ghproxy │ [切换] │  │
│  │ZZZeroUID │github.com/.../ZZZeroUID │ GitHub │ [切换] │  │
│  │SomePlugin│ (非 git 仓库)            │  未知   │  -     │  │
│  └──────────┴──────────────────────────┴────────┴────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### 推荐交互流程

#### 1. 页面加载

```
页面加载
  │
  ├─→ GET /api/git-mirror/info
  │     │
  │     ├─→ 渲染镜像源选择器
  │     │     - 使用 available_mirrors 渲染单选按钮组或下拉框
  │     │     - 高亮 current_mirror 对应的选项
  │     │     - 可根据 type 字段分组展示（镜像源 / 代理 / SSH）
  │     │
  │     └─→ 渲染插件列表表格
  │           - 展示每个插件的 name、remote_url、mirror 状态
  │           - is_git_repo 为 false 的行灰显，禁用操作按钮
  │           - mirror 字段映射为彩色标签
  │
  └─→ 页面就绪
```

#### 2. 一键替换所有插件

```
用户选择新镜像源 → 点击"一键应用到所有插件"
  │
  ├─→ 弹出确认对话框
  │     "确认将所有插件的 git remote URL 切换到 xxx？"
  │     如果选择 SSH 模式，额外提示：
  │     "SSH 模式需要已配置 GitHub SSH 密钥，确认继续？"
  │
  ├─→ 用户确认
  │     │
  │     └─→ POST /api/git-mirror/set-all
  │           {mirror_prefix: "https://gitcode.com/gscore-mirror/"}
  │           或 {mirror_prefix: "ssh://"}
  │           │
  │           ├─→ 显示结果摘要
  │           │     "成功切换 15 个插件，2 个跳过（非 git 仓库）"
  │           │
  │           └─→ 刷新插件列表
  │                 GET /api/git-mirror/info
  │
  └─→ 用户取消 → 无操作
```

#### 3. 单独切换某个插件

```
用户点击某行的"切换"按钮
  │
  ├─→ 弹出选择框（或使用当前选中的镜像源）
  │     选项: GitCode / CNB / ghproxy / SSH / GitHub (默认)
  │
  └─→ POST /api/git-mirror/set-plugin/{plugin_name}
        {mirror_prefix: "ssh://"}
        │
        ├─→ 成功 → 更新该行的 remote_url 和 mirror 标签
        │
        └─→ 失败 → 显示错误提示
```

### 错误处理建议

| 场景 | 建议处理方式 |
|------|-------------|
| 网络请求失败 | 显示 toast 错误提示，保留当前页面状态 |
| 部分插件切换失败 | 在结果摘要中显示失败数量，表格中对应行标红 |
| 插件不是 git 仓库 | 表格中灰显该行，禁用操作按钮，显示"非 git 仓库"提示 |
| 插件不存在（单个切换） | 显示 toast 错误提示 "插件 xxx 不存在" |
| SSH 模式但未配置密钥 | 切换后 git pull 会失败，建议在切换前提示用户确认 |

### 与核心配置 API 的关系

`GitMirror` 配置项也可以通过核心配置 API 进行查看和修改：

- **查看**：`GET /api/framework-config/GsCore` → 响应中包含 `GitMirror` 配置项
- **修改**：`POST /api/framework-config/GsCore/item/GitMirror` → `{ "value": "https://gitcode.com/gscore-mirror/" }`

但通过核心配置 API 修改**仅影响后续新安装的插件**，不会自动替换已安装插件的 git remote URL。如需同时替换已安装插件，请使用本节的 `/api/git-mirror/set-all` 接口。
