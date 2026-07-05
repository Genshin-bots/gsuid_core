# 43. 插件商店 API - /api/plugin-store

## 概述

插件商店 API 提供对插件的远程查询、安装、更新、卸载等管理能力。前端通过这套接口实现「插件商店」页面，让用户在不接触服务器的情况下完成插件的全生命周期管理。

**核心能力**：
- 获取远程插件商店列表（带本地安装状态）
- 通过插件 ID 安装插件（插件商店白名单内）
- **通过 URL 安装任意 git 仓库插件（不在商店内也可安装）**
- 更新已安装插件
- 卸载已安装插件

**技术特点**：
- `install` / `install-url` 完成后会自动 `reload_plugin`，使新插件立即生效
- `install` / `install-url` 复用 `install_plugins` 的 GitMirror 镜像源 / fallback 逻辑：用户配置镜像后，克隆优先走镜像，镜像未同步时自动回退到 GitHub 源
- 所有 git 操作通过 `asyncio.create_subprocess_exec` 异步执行，不会阻塞事件循环

**认证方式**：所有 API 均需通过 `Authorization: Bearer <token>` Header 携带访问令牌。

---

## 43.1 获取插件商店列表

从远程服务器拉取可用插件列表，并与本地已安装插件进行比对，返回每个插件的安装状态。

```
GET /api/plugin-store/list
```

**请求参数**：无

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "plugins": [
            {
                "id": "genshinuid",
                "name": "原神 UID",
                "description": "原神 UID 查询插件",
                "version": "latest",
                "author": "KimigaiiWuyi",
                "tags": ["genshin", "uid"],
                "icon": "https://...",
                "cover": "https://...",
                "avatar": "https://...",
                "link": "https://github.com/KimigaiiWuyi/GenshinUID",
                "branch": "main",
                "type": "tip",
                "content": "普通",
                "info": "原神 UID 查询插件",
                "installMsg": "请先安装依赖 xxx",
                "alias": ["GenshinUID", "gsuid"],
                "installed": true,
                "hasUpdate": false,
                "status": "installed"
            }
        ],
        "fun_plugins": [],
        "tool_plugins": []
    }
}
```

**字段说明**：

| 字段路径 | 类型 | 说明 |
|----------|------|------|
| `data.plugins` | `array` | 插件商店中的插件列表 |
| `data.plugins[].id` | `string` | 插件 ID，对应安装 / 卸载接口的 `plugin_id` |
| `data.plugins[].name` | `string` | 插件显示名 |
| `data.plugins[].link` | `string` | 插件仓库 URL（GitHub 等） |
| `data.plugins[].branch` | `string` | 默认分支 |
| `data.plugins[].installed` | `boolean` | 是否已在本地安装 |
| `data.plugins[].status` | `string` | `installed` / `not_installed` |
| `data.fun_plugins` | `array` | 娱乐插件分类 |
| `data.tool_plugins` | `array` | 工具插件分类 |

**错误响应**：
```json
{
    "status": 1,
    "msg": "获取插件列表失败: ...",
    "data": []
}
```

---

## 43.2 通过插件 ID 安装（插件商店白名单）

从插件商店中按 ID 安装插件。后端会根据 `plugin_id` 查找插件商店列表中对应的仓库 URL，然后 `git clone` 到 `plugins/` 目录并 `reload` 加载。

```
POST /api/plugin-store/install/{plugin_id}
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_id` | `string` | ✅ | 插件商店中的插件 ID（参见 §43.1 的 `data.plugins[].id`） |

**请求体**：
```json
{
    "repo_url": "https://github.com/owner/repo.git"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `repo_url` | `string` | ⚠️ | 预留字段，当前由后端根据 `plugin_id` 从插件商店解析。传错不影响主流程 |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "✅ 插件GenshinUID安装并加载成功!"
}
```

**错误响应**（插件不在商店中）：
```json
{
    "status": 1,
    "msg": "❌ 不存在插件 xxx, 请检查名称或使用[刷新插件列表]!"
}
```

**错误响应**（已安装）：
```json
{
    "status": 1,
    "msg": "❌ 该插件已经安装过了!"
}
```

---

## 43.3 通过 URL 安装（任意 git 仓库） ⭐

直接接受前端传来的 git 仓库 URL，安装**未收录在插件商店**中的自定义插件 / 内部仓库 / 第三方仓库。后端会从 URL 末段推导插件目录名、`git clone` 到 `plugins/` 目录并 `reload` 加载。

```
POST /api/plugin-store/install-url
```

**请求体**：
```json
{
    "url": "https://github.com/KimigaiiWuyi/GenshinUID.git",
    "branch": "main"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | `string` | ✅ | git 仓库 URL。支持 HTTP(S) / SSH(SCP)，末尾 `.git` 后缀可选。 |
| `branch` | `string` | ❌ | 指定克隆分支，默认使用仓库默认分支（如 `main` / `master`） |

**支持的 URL 格式**：

| 协议 | 示例 |
|------|------|
| HTTPS（推荐） | `https://github.com/owner/MyPlugin.git` |
| HTTPS（无 .git） | `https://github.com/owner/MyPlugin` |
| HTTPS（GitCode / CNB 等镜像） | `https://gitcode.com/owner/MyPlugin` |
| SSH（URL 形式） | `ssh://git@ssh.github.com:443/owner/MyPlugin.git` |
| SSH（SCP 形式） | `git@github.com:owner/MyPlugin.git` |

> **URL 协议限制**：仅支持 `http://` / `https://` / `ssh://` / `git@` 开头的 URL。`ftp://`、`file://` 等协议会被拒绝。

**插件目录名推导规则**：
从 URL 末段提取（去掉 `.git` 后缀），例如：
- `https://github.com/KimigaiiWuyi/GenshinUID.git` → `plugins/GenshinUID/`
- `https://github.com/KimigaiiWuyi/GenshinUID` → `plugins/GenshinUID/`
- `git@github.com:KimigaiiWuyi/GenshinUID.git` → `plugins/GenshinUID/`

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "✅ 插件MyPlugin安装并加载成功!"
}
```

**错误响应**（参数缺失）：
```json
{
    "status": 1,
    "msg": "❌ 请提供有效的 git 仓库 URL"
}
```

**错误响应**（协议不支持）：
```json
{
    "status": 1,
    "msg": "❌ URL 协议不支持, 当前仅支持 http://、https://、ssh://、git@ 开头的 git 仓库"
}
```

**错误响应**（URL 无法解析）：
```json
{
    "status": 1,
    "msg": "❌ 无法从 URL 中提取仓库名, 请检查 URL 是否合法"
}
```

**错误响应**（已安装同名插件）：
```json
{
    "status": 1,
    "msg": "❌ 该插件已经安装过了!"
}
```

**错误响应**（网络/克隆失败）：
```json
{
    "status": 1,
    "msg": "❌ 插件MyPlugin安装失败: 克隆失败: ..."
}
```

**错误响应**（克隆成功但加载失败）：
```json
{
    "status": 1,
    "msg": "❌ 插件MyPlugin已安装, 但加载失败, 可尝试[core重启]:\n❌ ..."
}
```

### 43.3.1 与 `install/{plugin_id}` 的区别

| 维度 | `install/{plugin_id}` | `install-url` |
|------|----------------------|---------------|
| 插件来源 | 必须在官方插件商店列表中 | 任意 git 仓库 |
| 入参 | 路径参数 `plugin_id` | body 字段 `url` |
| 用途 | 用户从商店页面一键安装 | 用户填入自建 / 第三方仓库 URL |

> 💡 **建议**：前端插件商店页面同时提供「商店一键安装」和「自定义 URL 安装」两个入口，分别调用 `install/{plugin_id}` 和 `install-url`。

### 43.3.2 镜像源与 fallback 行为

`install-url` 复用了 `install_plugins` 的完整链路：

1. **如果用户在「Git 镜像源管理」中配置了镜像**（详见 [25. Git 镜像源管理 API](./25-git-mirror.md)），后端会先尝试从镜像克隆；
2. **如果镜像未同步该仓库**（典型 401 / 403 / 404），会自动回退到原始 GitHub 源；
3. **如果用户未配置镜像**，直接使用用户提供的 URL 克隆。

---

## 43.4 更新已安装插件

```
POST /api/plugin-store/update/{plugin_id}
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_id` | `string` | ✅ | 已安装插件的名称 |

**请求参数**：无

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "插件更新成功"
}
```

**错误响应**：
```json
{
    "status": 1,
    "msg": "插件更新失败"
}
```

> **更细粒度的更新操作**（强制更新、回退版本、查看 commit 历史等）请使用 [28. Git 版本管理 API](./28-git-update.md)。

---

## 43.5 卸载已安装插件

```
DELETE /api/plugin-store/uninstall/{plugin_id}
```

**路径参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `plugin_id` | `string` | ✅ | 已安装插件的名称（大小写不敏感） |

**响应**（成功）：
```json
{
    "status": 0,
    "msg": "✅ 插件目录 MyPlugin 删除成功!"
}
```

**错误响应**（内置插件不可删除）：
```json
{
    "status": 1,
    "msg": "❌ 内置插件不可删除！"
}
```

**错误响应**（文件被占用，部分平台如 Windows 上偶发）：
```json
{
    "status": 1,
    "msg": "⚠️ 插件目录 MyPlugin 部分文件被锁定,请手动删除或重启后重试!"
}
```

---

## 前端集成指南

### 推荐页面布局

```
┌──────────────────────────────────────────────────────────────┐
│  插件商店                                                      │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  标签: [全部] [娱乐] [工具]                                    │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ [图标] 原神 UID                                         │ │
│  │ 作者: KimigaiiWuyi                                      │ │
│  │ 描述: 原神 UID 查询插件                                  │ │
│  │ 状态: ✅ 已安装                                          │ │
│  │ [卸载] [更新]                                            │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                              │
│  ─── 自定义 URL 安装 ──────────────────────────────────────  │
│  URL: [ https://github.com/owner/MyPlugin.git        ]      │
│  分支: [ main (可选)                                  ]      │
│  [ 📦 安装 ]                                                  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### 推荐交互流程

#### 1. 页面加载

```
页面加载
  │
  └─→ GET /api/plugin-store/list
        │
        ├─→ 渲染插件卡片列表
        │     - 标记 installed=true 的为「已安装」
        │     - 未安装的显示「安装」按钮
        │
        └─→ 渲染「自定义 URL 安装」表单
```

#### 2. 商店一键安装

```
用户点击某个未安装插件的「安装」按钮
  │
  └─→ POST /api/plugin-store/install/{plugin_id}
        │
        ├─→ 成功（status=0）
        │     - 显示成功 toast
        │     - 按钮文案变为「已安装」，可点击「卸载」/「更新」
        │     - 刷新列表 GET /api/plugin-store/list
        │
        └─→ 失败（status=1）
              - 显示错误 toast，msg 字段已是用户友好的多行格式
              - 按钮恢复为「安装」可重试
```

#### 3. 自定义 URL 安装（核心新增）

```
用户在「自定义 URL 安装」表单输入 URL，点击「安装」
  │
  ├─→ 前端校验：URL 非空、协议前缀合法
  │
  └─→ POST /api/plugin-store/install-url
        body: { url: "...", branch: "..." (可选) }
        │
        ├─→ 成功（status=0）
        │     - 显示成功 toast: "✅ 插件xxx安装并加载成功!"
        │     - 清空表单
        │     - 刷新列表 GET /api/plugin-store/list
        │
        └─→ 失败（status=1）
              - 显示错误 toast（msg 字段直接展示给用户）
              - 常见错误:
                · "❌ URL 协议不支持..." → 提示用户检查 URL
                · "❌ 无法从 URL 中提取仓库名..." → URL 格式有误
                · "❌ 该插件已经安装过了!" → 同名插件已存在
                · "❌ 插件xxx安装失败: 克隆失败: ..." → 网络/仓库问题
                · "❌ 插件xxx已安装, 但加载失败..." → 提示用户 core 重启
```

#### 4. 更新已安装插件

```
用户点击「更新」按钮
  │
  └─→ POST /api/plugin-store/update/{plugin_id}
        │
        ├─→ 成功 → 提示「更新成功」
        └─→ 失败 → 提示「更新失败」，建议改用「强制更新」
                  (POST /api/git-update/force-update/{plugin_name})
```

#### 5. 卸载已安装插件

```
用户点击「卸载」按钮
  │
  ├─→ 弹出二次确认对话框
  │
  └─→ 用户确认
        │
        └─→ DELETE /api/plugin-store/uninstall/{plugin_id}
              │
              ├─→ 成功 → 刷新列表
              └─→ 失败 → 提示用户手动删除或重启后重试
```

### 错误处理建议

| 场景 | 建议处理方式 |
|------|-------------|
| 网络/克隆失败 | 显示 `msg` 字段（已包含 git 原始错误），建议检查网络 / 镜像源配置 |
| 已安装同名插件 | 提示用户「已存在同名插件」，提供「前往已安装列表」入口 |
| URL 协议不支持 | 提示用户「仅支持 HTTP(S) / SSH 协议的 git 仓库」 |
| 内置插件不可删除 | 禁用卸载按钮 + tooltip 说明 |
| Windows 文件被占用 | 提示用户「部分文件被锁定，请重启后重试或手动删除」 |
| 克隆成功但加载失败 | 提示用户「插件已下载但加载失败，建议 core 重启」 |

### 与 Git 版本管理的关系

| 需求 | 推荐接口 |
|------|----------|
| 安装 / 卸载 / 列表 | `plugin-store/*`（本篇） |
| 查 commit 历史、回退版本、强制更新 | [28. Git 版本管理 API](./28-git-update.md) |
| 切换 Git 镜像源 | [25. Git 镜像源管理 API](./25-git-mirror.md) |
