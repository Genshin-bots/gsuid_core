# 九、插件管理体系

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[八、WebConsole](./08-webconsole.md) · **下一章**：[十、链接 Bot 适配器清单](./10-bots.md)

GsCore 本身只是个壳，**所有业务功能都来自插件**。本章讲部署者怎么装 / 卸 / 更
插件、管理 Git 镜像源、设置自动更新策略。

源码集中在 [`gsuid_core/utils/plugins_update/`](../../../gsuid_core/utils/plugins_update/) 与
[`gsuid_core/buildin_plugins/core_command/install_plugins/`](../../../gsuid_core/buildin_plugins/core_command/install_plugins/)。

## 9.1 插件的三种来源

| 来源 | 路径 | 形态 |
|------|------|------|
| **官方核心内置插件** | [`gsuid_core/buildin_plugins/`](../../../gsuid_core/buildin_plugins/) | 跟 Core 一起发布（core_command / core_help / core_pm / core_restart / install_plugins / user_login / core_status / core_ai_control / core_webconsole / auto_update / core_backup / core_update 等），不可单独卸 |
| **业务插件** | `gsuid_core/plugins/<plugin_name>/` | 独立 git 仓库，每个插件一个目录，由部署者手动 `git clone` 或通过 `core安装插件` 拉 |
| **第三方插件** | 同上 | 社区开发者发布，通过 `core安装插件` 自动索引拉 |

## 9.2 命令行安装（推荐）

> 需 master 权限（pm=0）。

```
core安装插件GenshinUID
```

执行流程（[`buildin_plugins/core_command/install_plugins/__init__.py`](../../../gsuid_core/buildin_plugins/core_command/install_plugins/__init__.py)）：

1. `get_plugins_url("genshinuid")` → 查 [`https://docs.sayu-bot.com/plugin_list.json`](../../../gsuid_core/utils/plugins_update/api.py)
2. 返回 `{git_url: ..., branch: ...}` → `install_plugins(plugins)`
3. 实际 git 操作由 [`git_async.py`](../../../gsuid_core/utils/plugins_update/git_async.py) 执行
4. **重要**：默认从 `cnb.cool` 镜像源拉，**若镜像源未同步该插件自动 fallback 到 GitHub**（见最近一次 commit [`984e0c6`](https://github.com/Genshin-bots/gsuid_core/commit/984e0c6) 引入的 `_is_mirror_not_synced_error()`）
5. 成功后提示 `core重启` 加载新插件

> 想换镜像源：WebConsole → 插件管理 → 右上角「切换 Git 源」（见 §9.5）。

## 9.3 手动安装

```sh
cd gsuid_core
cd plugins

# GenshinUID v4
git clone -b v4 https://github.com/KimigaiiWuyi/GenshinUID.git --depth=1 --single-branch

# StarRailUID
git clone https://github.com/baiqwerdvd/StarRailUID.git --depth=1 --single-branch

# 任意第三方
git clone https://github.com/xxx/YYYUID.git --depth=1 --single-branch

cd ../
```

手动装完需要 `core重启` 加载（或重启 Core 进程）。

> 也可以放到**任意路径**通过 `core_config.json` 的 `PLUGIN_PATH`（如果有）注册，
> 但一般无需折腾，放 `plugins/` 即可。

## 9.4 卸载 / 更新 / 强制更新

| Bot 命令 | 行为 |
|----------|------|
| `core卸载插件<名字>` | 二次确认（输入 `Y`）→ 删插件目录 |
| `core刷新插件列表` | 重新拉 `plugin_list.json` |
| `core更新插件` | git pull 所有插件 |
| `core更新插件<名字>` | 只更新指定插件 |
| `core强制更新插件<名字>` | 丢弃本地修改，hard reset |
| `core强行强制更新插件<名字>` | 先 `git clean -xdf` 再 hard reset（最暴力） |

> ⚠️ 不要在 Core 仓库根目录手动 `git rm` / `git clean` / `git reset --hard`！
> 这些操作可能误删 Core 自身代码。

## 9.5 Git 镜像源切换

镜像源定义在 [`gsuid_core/utils/plugins_update/git_mirror.py`](../../../gsuid_core/utils/plugins_update/git_mirror.py)：

```python
MIRROR_PREFIXES = {
    "https://gitcode.com/gscore-mirror/": "gitcode",
    "https://cnb.cool/gscore-mirror/": "cnb",
}

PROXY_PREFIXES = {
    "https://ghproxy.mihomo.me/": "ghproxy",
}

SSH_GITHUB_TEMPLATE = "ssh://git@ssh.github.com:443/{owner}/{repo}.git"
```

**三种模式**：

1. **镜像模式**（`gitcode` / `cnb`）：替换 `{prefix}/{repo_name}`
2. **代理前缀模式**（`ghproxy`）：拼接 `{proxy_prefix}{full_github_url}`
3. **SSH 模式**：`ssh://git@ssh.github.com:443/{owner}/{repo}.git`

**切换方法**：

- WebConsole：插件管理 → 右上角「切换 Git 源」
- 命令行：等效接口 `git_mirror` 在 WebConsole 实现

### 9.5.1 镜像源未同步的自动 fallback

最近一次 commit [`984e0c6`](https://github.com/Genshin-bots/gsuid_core/commit/984e0c6)
引入了 `_is_mirror_not_synced_error()`，关键字：

```
repository not found / not found / 404 / 401 / 403
authentication failed / permission denied / access denied / forbidden
credential / username / could not read / terminal prompts disabled
```

匹配到上述错误即判定「镜像源就是没这个仓库」，自动 fallback 到 GitHub 源。

> 网络抖动 / 超时 / DNS 等临时错误**不会**触发 fallback，避免无意义回退。

## 9.6 代理设置（不走镜像时）

`core_config.json`：

```json
{
  "ProxyURL": "https://gh-proxy.com"
}
```

填了之后，`core安装插件xxx` / 自动更新会用 `https://gh-proxy.com/{github_url}`
的形式拉代码（仅 git 协议层，HTTP 代理走 docker 的 `http_proxy` 环境变量）。

## 9.7 自动更新（定时）

`core_config.json` 默认配置：

```json
{
  "AutoUpdateCore": true,
  "AutoUpdatePlugins": true,
  "AutoRestartCore": false,
  "AutoUpdateCoreTime": ["3", "40"],
  "AutoUpdatePluginsTime": ["4", "10"],
  "AutoRestartCoreTime": ["4", "40"]
}
```

行为：

- 3:40 自动 `git pull` Core 仓库（**不重启**，需配合 `AutoRestartCore` 或手动重启）
- 4:10 自动 `git pull` 所有插件
- 4:40 若 `AutoRestartCore=true` 则触发 `core重启`（先 `core重启` 自己 → 拉起新进程）

> 自动更新**仅同步代码**，依赖变更（`pyproject.toml` 加了新包）需要手动
> `uv sync`，否则 ImportError。

## 9.8 插件加载流程

```
core.py main()
  └─ load_gss(dev)
       ├─ 扫描 gsuid_core/plugins/*/ 的 __init__.py
       ├─ 解析每个插件的 CONFIG_DEFAULT / StringConfig
       ├─ 写入 plugins_configs/<plugin>.json（如缺失）
       └─ 注册 SV / 触发器 / 启动钩子

applife lifespan
  └─ core_start_execute()
       ├─ on_core_start_before 阶段：数据库迁移 / exec_list
       ├─ on_core_start 阶段：插件逻辑初始化
       └─ on_core_start_after 阶段：WebConsole / API 启用
```

## 9.9 插件版本管理

- 每个插件是独立 git 仓库，commit 信息通过 `git_get_current_commit()` 记录在
  [`plugin_commit_versions`](../../../gsuid_core/utils/plugins_update/_plugins.py) 中
- 仅运行时持有，不持久化
- WebConsole「插件管理」页签可看到 commit hash

## 9.10 常见错误

| 现象 | 原因 / 解决 |
|------|--------------|
| `core安装插件<name>` 提示「不存在该插件」 | 名字拼错；先 `core刷新插件列表` |
| 安装成功但命令没出现 | 没 `core重启`；或插件加载报错，看日志 |
| 拉不动 / 卡死 | 切镜像源 / 配 `ProxyURL` / Docker 内设 git 代理 |
| `fatal: unable to access ... terminal prompts disabled` | 镜像源 401/403，自动 fallback 到 GitHub（已实现） |
| 强更失败提示「合并冲突」 | 用「强行强制更新」 |
| 卸载时二次确认收不到 | 在 DM（私聊）里发命令，群里要求 `@Bot` |
