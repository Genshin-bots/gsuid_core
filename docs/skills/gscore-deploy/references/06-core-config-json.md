# 六、`core_config.json` 字段详解

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[五、`config.json` 字段详解](./05-config-json.md) · **下一章**：[七、WebSocket 安全](./07-security-ws.md)

`data/core_config.json` 是 Core 的**行为开关集**，涵盖自动更新、自动重启、风控
文案、文本转图、镜像源、代理等。**强烈建议通过 WebConsole 改**——它会自动写
回 + 给字段补默认值。

字段定义分散在 [`gsuid_core/utils/plugins_config/`](../../../gsuid_core/utils/plugins_config/) 下：

| 模块 | 涵盖字段 |
|------|----------|
| [`config_default.py`](../../../gsuid_core/utils/plugins_config/config_default.py) | 核心行为（restart_command / MhySSLVerify / AutoUpdate* / 风控文案 / 转图 / 插件代理） |
| [`log_config.py`](../../../gsuid_core/utils/plugins_config/log_config.py) | 日志清理策略 |
| [`pass_config.py`](../../../gsuid_core/utils/plugins_config/pass_config.py) | 「神奇 API」「MysPass」等历史遗留 |
| [`backup_config.py`](../../../gsuid_core/utils/plugins_config/backup_config.py) | 自动备份路径 / 策略 |
| [`status_config.py`](../../../gsuid_core/utils/plugins_config/status_config.py) | Core 状态报告 |
| [`pic_gen_config.py`](../../../gsuid_core/utils/plugins_config/pic_gen_config.py) | 图片生成相关 |
| [`send_pic_config.py`](../../../gsuid_core/utils/plugins_config/send_pic_config.py) | 发图行为 |
| [`pic_server_config.py`](../../../gsuid_core/utils/plugins_config/pic_server_config.py) | 图片转公网链接 |
| [`sp_config.py`](../../../gsuid_core/utils/plugins_config/sp_config.py) | 帮助模式 / 同人 CD / 黑名单 / 合并转发 |
| [`database_config.py`](../../../gsuid_core/utils/plugins_config/database_config.py) | 数据库（见 [十一、数据库配置](./11-database.md)） |
| [`buttons_and_markdown_config.py`](../../../gsuid_core/utils/plugins_config/buttons_and_markdown_config.py) | 按钮 / Markdown 发送平台 |

## 6.1 最常用的字段（速查表）

> 在线文档对应章节：[GenshinUID-docs/Advance/CoreConfig.md](../../../../../GenshinUID-docs/docs/Advance/CoreConfig.md)

| 字段 | 默认 | 类型 | 说明 |
|------|------|------|------|
| `restart_command` | `"uv run python"` | str | `core重启` 实际执行的命令（可改为 `poetry run python` / `pdm run python`） |
| `MhySSLVerify` | `true` | bool | 米游社请求 SSL 校验（关闭可绕过部分过期证书问题） |
| `CaptchaPass` | `false` | bool | 已废弃 / 危险，**保持 false** |
| `MysPass` | `false` | bool | 已废弃 / 危险，**保持 false** |
| `AutoUpdateCore` | `true` | bool | 每天 3:40 自动 `git pull` core |
| `AutoUpdatePlugins` | `true` | bool | 每天 4:10 自动 `git pull` 所有插件 |
| `AutoRestartCore` | `false` | bool | 每天 4:40 自动 `core重启` |
| `AutoUpdateCoreTime` | `["3", "40"]` | List[str] | 时、分 |
| `AutoUpdatePluginsTime` | `["4", "10"]` | List[str] | 时、分 |
| `AutoRestartCoreTime` | `["4", "40"]` | List[str] | 时、分 |
| `AutoAddRandomText` | `false` | bool | core 发送文字时随机加尾巴（避免风控） |
| `RandomText` | `"abcdefghijklmnopqrstuvwxyz"` | str | 随机字符源 |
| `ChangeErrorToPic` | `true` | bool | 部分报错转图片 |
| `AutoTextToPic` | `false` | bool | 所有文字消息转图（非 QQ 平台不建议） |
| `TextToPicThreshold` | `"20"` | str | 转图阈值（字符数） |
| `EnableSpecificMsgId` | `false` | bool | 启用「特殊 msgid」（不清楚勿开） |
| `SpecificMsgId` | `""` | str | 特殊 msgid 值 |
| `AutoUpdateDep` | `false` | bool | 更新插件时同步 `pip install` 新依赖（多数情况不建议） |
| `EnablePicSrv` | `false` | bool | 把图片转公网链接（需 GsCore 所在服务器有公网 IP） |
| `PicSrv` | `""` | str | 公网域名前缀，如 `http://1.2.3.4:8765` |
| `ProxyURL` | `""` | str | 装插件时 git 走代理，如 `https://gh-proxy.com` |
| `SendMDPlatform` | `[]` | List[str] | 允许发送 MD 消息的平台 |
| `SendButtonsPlatform` | `["villa","kaiheila","dodo","discord","telegram","qqgroup","qqguild"]` | List[str] | 允许按钮的平台 |
| `SendTemplatePlatform` | `["qqgroup","qqguild"]` | List[str] | 用模板方式发按钮 / MD 的平台 |
| `TryTemplateForQQ` | `true` | bool | 启用后读 `data/template/` 下的模板发 |
| `ForceSendMD` | `false` | bool | 强制 MD 发图文 |
| `UseCRLFReplaceLFForMD` | `true` | bool | MD 消息 `\n` → `\r`（QQ 模板需要） |
| `ShieldQQBot` | `["38890","28541","28542"]` | List[str] | 含这些官 Bot QQ 时不响应（避免和官 Bot 打架） |
| `ScheduledCleanLogDay` | `"8"` | str | 日志保留天数 |

## 6.2 核心行为类

### 6.2.1 `restart_command`

`core重启` 实际执行的 shell 命令。默认 `uv run python`：

- 若用 poetry：`poetry run python`
- 若用 pdm：`pdm run python`
- 若 Docker：通常**不用这个字段**，因为 Docker 镜像默认 `CMD ["uv", "run",
  "--python", "/venv/bin/python", "core", "--host", "0.0.0.0"]`，重启走
  `docker restart gsuid_core`。

源码：[`gsuid_core/buildin_plugins/core_command/core_restart/restart.py`](../../../gsuid_core/buildin_plugins/core_command/core_restart/restart.py)

### 6.2.2 米游社相关

- `MhySSLVerify`：false 后会绕过米游社 SSL 校验，临时解决「证书过期」类问题，但
  不安全。
- `MysPass` / `CaptchaPass`：历史绕过风控开关，**当前版本无效且有一定风险**，
  保持 `false`。

## 6.3 自动更新与重启策略

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

> **最佳实践**（生产环境）：
>
> - `AutoUpdateCore=true` + `AutoUpdatePlugins=true` → 拉新代码
> - `AutoRestartCore=true` → 凌晨 4:40 自动重启让更新生效
> - **配合** Docker `--restart always` 或 systemd `Restart=always` 兜底
>
> 注意：自动更新**仅拉代码**，依赖变更（`pyproject.toml` 改了）需要
> `docker exec -it gsuid_core uv sync` 手动同步，否则可能 ImportError。

### 6.3.1 镜像源 + 自动更新策略

`core_config.json` 的 `ProxyURL` 字段：

```json
{ "ProxyURL": "https://gh-proxy.com" }
```

填写后，`core安装插件xxx` / 自动更新命令会通过这个 git 代理前缀拉代码。

更细粒度的镜像源切换（gitcode / cnb / ghproxy / SSH）走
[九、插件管理体系 §9.5](./09-plugins.md#95-git-镜像源切换)。

## 6.4 风控文案相关

```json
{
  "AutoAddRandomText": false,
  "RandomText": "abcdefghijklmnopqrstuvwxyz"
}
```

`AutoAddRandomText=true` 后，Core 发送的**每条文字**末尾会随机加 1~2 个
`RandomText` 里的字符，避免平台风控认为「完全相同的消息」。**仅 QQ 平台有意义**。

## 6.5 转图与渲染

```json
{
  "ChangeErrorToPic": true,
  "AutoTextToPic": false,
  "TextToPicThreshold": "20"
}
```

- `ChangeErrorToPic`：把部分统一报错（如绑定类提示）渲染成图片。
- `AutoTextToPic`：所有文字消息转图。**仅在担心 QQ 文字风控时开启**，但会增加
  大量图片生成开销。
- `TextToPicThreshold`：超过该字符数才转图（默认 20）。

## 6.6 平台适配

| 字段 | 含义 |
|------|------|
| `SendMDPlatform` | 哪些平台允许发 Markdown 消息（villa / kaiheila / dodo / discord / telegram / qqgroup / qqguild / ...） |
| `SendButtonsPlatform` | 哪些平台允许按钮 |
| `SendTemplatePlatform` | 哪些平台走模板（QQ 群 / 频道官方 API 模板） |
| `TryTemplateForQQ` | 启用后读 `data/template/` 模板发 |
| `ForceSendMD` | 强制 MD 发图文（QQ 默认关，因为纯图文走默认更稳） |
| `UseCRLFReplaceLFForMD` | MD 消息 `\n` 替换为 `\r`（QQ 模板限制） |
| `ShieldQQBot` | 含这些官 Bot QQ 时不响应 |

> 一般用户无需调整，**升级后插件默认行为变化时**再来配。

## 6.7 图片转公网链接

```json
{
  "EnablePicSrv": false,
  "PicSrv": "http://1.2.3.4:8765"
}
```

启用后 Core 发送的图片会被替换为 `PicSrv + 图片路径` 的 HTTP URL。**要求 Core
所在机器有公网 IP 或域名**，否则图片会被下载不到。

## 6.8 帮助 / 状态相关（SP 配置）

`SP_CONIFG`（[`sp_config.py`](../../../gsuid_core/utils/plugins_config/sp_config.py)）：

```json
{
  "HelpMode": "dark",
  "AtSenderPos": "消息最前",
  "SameUserEventCD": 0,
  "BlackList": [],
  "ShieldQQBot": ["38890", "28541", "28542"],
  "EnableForwardMessage": "允许"
}
```

- `HelpMode`：帮助图主题（dark / light）
- `AtSenderPos`：`@发送者` 的位置（消息最前 / 消息最后）
- `SameUserEventCD`：同用户两次触发同一命令的最小间隔秒数（0 不限）
- `BlackList`：黑名单用户 / 群，所有命令不响应
- `EnableForwardMessage`：合并转发策略（允许 / 禁止 / 合并为一条消息 / 1~5 / 全部拆成单独消息）

## 6.9 日志清理

```json
{ "ScheduledCleanLogDay": "8" }
```

`data/logs/` 下的 `.log` 文件超过 8 天自动删。

## 6.10 数据库配置（速查，详细见 [十一、数据库配置](./11-database.md)）

```json
{
  "db_type": "SQLite",
  "db_driver": "aiomysql",
  "db_host": "localhost",
  "db_port": "3306",
  "db_user": "root",
  "db_password": "root",
  "db_name": "GsData",
  "db_pool_size": 5,
  "db_echo": false,
  "db_pool_recycle": 1500,
  "db_custom_url": ""
}
```

切换 MySQL 步骤：

1. `uv pip install aiomysql`（或 `asyncmy`，看 `db_driver`）
2. WebConsole → 插件管理 → 修改插件设定 → GsCore 数据库配置 → 改 `db_type` 等
3. 重启 Core

## 6.11 备份 / 状态 / 图片生成

这几个分类的配置**一般保持默认**，出问题再调：

- `backup_config.py`：自动备份路径、cron、最大保留份数
- `status_config.py`：状态报告的开关与渠道
- `pic_gen_config.py`：图片生成的字体、超时、并发
- `send_pic_config.py`：发图策略（quality / format / size limit）
- `pic_server_config.py`：图片转公网（见 §6.7）

## 6.12 修改建议

1. **用 WebConsole 改**（左侧菜单「核心配置」页签）
2. 改完看「是否需要重启」提示
3. 大批量改动建议先 `cp data/core_config.json data/core_config.json.bak`
4. **字段缺失会被自动补默认值**，坏 JSON 会导致 Core 启动失败
