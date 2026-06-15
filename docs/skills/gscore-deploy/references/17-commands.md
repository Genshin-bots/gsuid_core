# 十七、常用内置命令速查

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十六、故障排查清单](./16-troubleshooting.md)

GsCore 内置一套「Core 管理」命令，**所有命令都需要 master 权限**（pm=0），
除非另注。这些命令由 [`gsuid_core/buildin_plugins/core_command/`](../../../gsuid_core/buildin_plugins/core_command/) 提供。

> `command_start`（`config.json`）为空时，下面命令直接发；填了的话前面必须带命令头。

## 17.1 Core 管理（`core` 前缀）

| 命令 | 别名 | 功能 |
|------|------|------|
| `core重启` | `core重启` | 保存状态 → 触发 `restart_command` → 拉起新进程 |
| `core关闭` | `core关闭Core` | 保存状态后 `os._exit(0)`（依赖外部守护拉起） |
| `core状态` | `gs状态` | 输出 Core 启动耗时 / 插件数 / 服务数 / 触发器数 / AI 统计 |
| `core帮助` | `core菜单` | 输出内置命令菜单 |
| `core全部更新` | `gs全部更新` | 更新 Core + 所有插件 |
| `core强制更新` | `gs强制更新` | 强制更新（丢弃本地修改） |
| `core更新` | `gs更新` | `git pull` 当前目录（即 Core 自身） |

源码：[`gsuid_core/buildin_plugins/core_command/core_restart/`](../../../gsuid_core/buildin_plugins/core_command/core_restart/) 等。

## 17.2 插件管理（`core` 前缀）

| 命令 | 功能 |
|------|------|
| `core安装插件<名字>` | 从 `plugin_list.json` 拉插件到 `plugins/` |
| `core卸载插件<名字>` | 二次确认（输入 `Y`）→ 删插件目录 |
| `core更新插件` | git pull 所有插件 |
| `core更新插件<名字>` | git pull 指定插件 |
| `core强制更新插件<名字>` | 丢弃本地修改 |
| `core强行强制更新插件<名字>` | `git clean -xdf` 后 hard reset（最暴力） |
| `core刷新插件列表` | 重新拉 `plugin_list.json` |

源码：[`gsuid_core/buildin_plugins/core_command/install_plugins/__init__.py`](../../../gsuid_core/buildin_plugins/core_command/install_plugins/__init__.py)

## 17.3 权限 / 用户管理

| 命令 | 功能 |
|------|------|
| `core权限` | 查看自己当前 PM |
| `core设置权限<pm>` | 改某用户的 PM（仅 master） |
| `core添加主人<user_id>` | 加主人（pm=0） |
| `core移除主人<user_id>` | 移除主人 |
| `core添加超管<user_id>` | 加超级用户（pm=1） |

> 实际操作是改 `config.json` 的 `masters` / `superusers`。

## 17.4 配置 / 资源

| 命令 | 功能 |
|------|------|
| `core配置` | 输出当前核心配置摘要 |
| `core重置配置` | 把 `config.json` 重置成 `CONFIG_DEFAULT`（**慎用**，会清 masters / WS_TOKEN） |
| `core重置core配置` | 同上 |
| `core查看sv` | 输出 sv 权限矩阵 |
| `core查看插件配置` | 列出所有 `plugins_configs/*.json` |
| `core下载全部资源` | 触发各插件的资源补齐 |
| `core清理日志` | 立即清理过期日志 |

> 不同插件**自身**的命令（如 GenshinUID 的 `gs签到` / `gs体力`）由各插件实
> 现，详见插件自身文档。

## 17.5 数据库 / 备份

| 命令 | 功能 |
|------|------|
| `core备份` | 立即触发 WebConsole「备份管理」里的备份策略 |
| `core查看备份` | 列出自动备份文件 |
| `core恢复<文件名>` | 从备份恢复（**慎用**） |

源码：[`gsuid_core/buildin_plugins/core_command/core_backup/`](../../../gsuid_core/buildin_plugins/core_command/core_backup/)。

## 17.6 WebConsole 账号

WebConsole 账号**不走 Bot 命令**，在 `http://HOST:PORT/app` 网页里操作：

- 首次：用 `REGISTER_CODE` 注册管理员（只能注册一个）
- 改密 / 改邮箱 / 头像：账号设置页
- 多用户（计划中）：当前不支持

详见 [八、WebConsole](./08-webconsole.md)。

## 17.7 AI 命令（若启用 AI）

| 命令 | 功能 |
|------|------|
| `ai <问题>` | 走 `ai_mode` 触发 AI 应答 |
| `@Bot <问题>` | 同上（`ai_mode` 含「提及应答」） |
| `ai 清空对话` | 清当前会话历史 |
| `ai 设置人格<名字>` | 切换人格 |
| `ai 状态` | 查看 AI 配置 / 当前模型 / token 消耗 |

具体看 [十三、AI 核心部署要点](./13-ai.md) 与用户文档 AI 章节。

## 17.8 帮助系统

插件可以在 `register_help` 时注册命令到主帮助菜单，用户用 `gs帮助` / `core帮助`
输出。

源码：[`gsuid_core/buildin_plugins/core_command/core_help/`](../../../gsuid_core/buildin_plugins/core_command/core_help/)

> 帮助图改完**不会自动刷新**，需要 `core重启` 重新生成。

## 17.9 PM 等级速查

| pm | 含义 |
|----|------|
| 0 | 主人（`masters`） |
| 1 | 超级用户（`superusers`） |
| 2 | 群主 / 频道主 |
| 3 | 群管理员 |
| 4 | 频道管理员 |
| 5 | 子频道管理员 |
| 6 | 普通用户 |

> WebConsole 部分版本把 `masters` 显示为「管理员列表」——这里的"管理员"指**主人**
> (pm=0)，不是群管理员（pm=3）。

## 17.10 命令头（`command_start`）

`config.json` 的 `command_start` 字段：

| 值 | 行为 |
|----|------|
| `[]`（默认） | 无命令头，所有消息直接匹配命令 |
| `[""]` | 等价 `[]` |
| `["*"]` | 所有命令必须以 `*` 开头（如 `*gs帮助`） |
| `["/", "!"]` | 以 `/` 或 `!` 开头均可 |
| `["/", ""]` | 允许带命令头，也可不带（一般没必要） |

> 改了之后 Core 必须重启；填错会导致**所有命令**都无法触发，包括 Bot 内 master
> 命令（这时只能改文件 + 重启）。

## 17.11 黑名单 / 白名单

`config.json` 的 `sv.<service>.black_list` / `white_list`：

- `black_list`：`["user_id", "group_id"]`，这些用户 / 群对该服务不响应
- `white_list`：留空 = 所有用户可用；非空 = 仅白名单可用
- 同时存在：`white_list` 生效，`black_list` 不生效

全局黑名单（`core_config.json` 的 `BlackList`）：用户 / 群**完全**不会触发
任何命令。

## 17.12 同用户命令 CD

`core_config.json` 的 `SameUserEventCD`：

```json
{ "SameUserEventCD": 5 }
```

同用户两次触发同一命令最小间隔 5 秒（0 = 不限）。**防刷屏**。

## 17.13 常用命令路径速查

```
内置插件路径：
gsuid_core/buildin_plugins/core_command/
├── core_restart/        ← core重启 / core关闭
├── core_status/         ← core状态
├── core_help/           ← core帮助
├── core_pm/             ← core权限 / 加主人
├── core_user/           ← core用户管理
├── core_config_cmd/     ← core配置 / core重置配置
├── core_update/         ← core更新
├── core_backup/         ← core备份
├── auto_update/         ← AutoUpdate* 定时任务
├── core_ai_control/     ← AI 命令
├── core_webconsole/     ← WebConsole 入口
├── install_plugins/     ← 装/卸/更/刷新插件
└── user_login/          ← 用户登录（如有）
```

源码里每个子目录都是独立插件，可以单独看实现。
