# 三、触发器、消息收发、渲染、帮助与状态

> 返回 [SKILL 主入口](../SKILL.md)

本节核查插件是否会干扰其他插件或 AI 对话流、命令权限边界是否清晰、出图方案是否轻量可靠、以及用户可见的帮助与文档是否准确。

## 3.1 触发器与消息流隔离

对照 [02-sv-and-triggers.md](../../gscore-plugin-development/references/02-sv-and-triggers.md) 审查所有 `Plugins`、`SV` 与 `@sv.on_*`：

| 检查项 | 级别 | 审核要点 |
|--------|------|---------|
| 前缀约束 | P0 | `Plugins` 应声明 `force_prefix` 且 `allow_empty_prefix=False`；触发器除极特殊场景外不得设 `prefix=False` |
| 高污染触发器 | P0 | 严查 `on_keyword`、`on_message`、`on_regex(r".*")`：无前缀或宽泛正则会截胡群聊普通对话、阻断 AI 与其他插件；`on_message` 绝不可无条件 `bot.send()` |
| 敏感命令权限 `pm` | P0 | 全员批量操作（如「全部签到」）、清缓存、改全局配置、刷数据管理命令必须放在独立 `SV(..., pm=0/1/2)`，不得与普通用户 `pm=6` 共用同一个 SV |
| 触发器选型与冲突 | P1 | 精确无参命令用 `on_fullmatch(..., block=True)`；可选参数用 `on_command`；必带参数用 `on_prefix`；同 SV 内注意长短命令与正则重叠 |
| 元事件 `on_meta` | P1 | 仅订阅三种标准事件（`user_join_group` / `user_exit_group` / `poke`）；可选字段用 `ev.get_meta(key)` 做 `None` 守卫；不依赖 `ev.sender` 昵称或 `ev.msg_id` 引用回复 |
| 签名与 Bot 封装 | P0 | 触发器签名固定为 `async def fn(bot: Bot, ev: Event)`；禁止直接导入或构造 `_Bot` |

## 3.2 消息收发与跨平台兼容

1. **多平台兼容（P1）**：
   - `at_sender=True` / `MessageSegment.at(ev.user_id)` 在私聊场景应按 `bool(ev.group_id)` 控制，避免部分平台私聊发 `@` 异常或冗余；
   - 发送大文件或 `file://` URI 时（如回放文件），默认应关闭 `file://`（走 base64 通用路径），开启 `file://` 须作为显式配置项并注明「要求协议端本机可达」，且路径必须锁定在插件自身缓存目录内（防止路径穿越）。
2. **撤回 / 禁言（P1）**：调用 `wait_recall=True` / `bot.unsend()` / `bot.ban()` 时需容忍返回 `None`（HTTP 模式与部分平台不支持），且不得滥用禁言。
3. **长耗时命令交互体验（P1）**：耗时 10 秒以上的命令（如拉取远端战绩快照、批量签到），建议先回执提示或后台完成后推送，但要确保后台任务持有强引用（防 GC 回收）并捕获业务异常给用户反馈。
4. **命令内 `bot.send(bytes)` 说明**：框架 `convert_message()` 会对裸 `bytes` 调用 `MessageSegment.image()`（含 `IS_UPLOAD` 图床逻辑），直接发 PNG/JPEG 字节流是合法的；若发送 `PIL.Image` 对象则须经 `convert_img()` 转换。

## 3.3 图片渲染范式与模板安全

对照 [09-image-rendering.md](../../gscore-plugin-development/references/09-image-rendering.md) 核查：

| 档位 | 优先级 | 审核关注点 |
|------|--------|-----------|
| ① PIL | 首选 | 必须用 `core_font()`；大图合成/高斯模糊放 `@to_thread`；及时关闭 `Image.open` 句柄（用 `with Image.open(...)`） |
| ② pytakumi | 推荐 | 调 `gsuid_core.utils.html_render.render_html_to_bytes` / `render_md_to_bytes`；检查传参是否与当前 Core 签名一致；本地图片须转 data URI 嵌入 |
| ③ playwright | 兜底 | 仅需 JS/ECharts/Plotly 时才允许；必须声明依赖并在 README 写明 `playwright install chromium`；检查 `browser.close()` 是否在 `finally` / `async with` 释放防僵尸进程 |

**HTML/Jinja2 模板安全检查（P0/P1）**：
- `jinja2.Environment` 必须开启 `autoescape=True`（防外部玩家昵称/聊天内容注入恶意 HTML/XSS）；
- 检查全部 `.html` 模板是否使用 `| safe`、`<script>`、`<iframe>` 或直接引用不可信外部 URL；
- 若模板面向用户浏览器（如登录页）而非 `pytakumi`，任何嵌入 `<script>` 的变量必须用 `| tojson`，且静态外链仅限明确可信官方域名。

## 3.4 帮助系统、`core状态` 与文档一致性

对照 [08-help-system.md](../../gscore-plugin-development/references/08-help-system.md) 核查：

1. **`register_help` 与 `get_new_help`（P1）**：
   - 是否在帮助模块调用 `register_help(plugin_name, f"{get_plugin_available_prefix(...)}帮助", icon)`；
   - `help.json` 中每个条目的 `name` 是否对应 `icon_path/<name>.png`（若有图标目录），检查拼写错别字；
   - 若开启 `enable_cache=True` 且支持热更新背景图，背景更新后应清理帮助图缓存，否则重启前不会生效；
   - 模块 import 阶段尽量不要做重型同步图像处理（如全图高斯模糊），优先延迟到 `on_core_start` 或首次请求时 `@to_thread`。
2. **`register_status`（P1）**：
   - 公开商店插件建议注册 1–4 个核心运行指标（绑定数、登录数、订阅数等）到 `core状态`；
   - 指标函数必须 `async` 且只查本地库/内存，**严禁**调用外部 HTTP API（会拖慢整个 `core状态` 渲染）。
3. **README / `help.json` / 实际触发器三方核对（P1）**：
   - 逐条对照 README 命令表、`help.json` 与代码 `@sv.on_*` 关键词，确认无漏记、无多写、无命令字不匹配；
   - **真实案例（`CalabiyauUID`）**：
     - [README.md:68](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/README.md#L68) 写为 `kq战队jmcomic`，而代码 [calabiyau_clan/__init__.py:23](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/calabiyau_clan/__init__.py#L23) 实际是 `@sv_clan.on_command("战队查询")`（用户照 README 发命令无响应）；
     - [help.json:183](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/calabiyau_help/help.json#L183) 出现错别字 `"团竟排行"`（且图标文件同名，改名时需同步改图标文件名）；
     - 全插件未调用 `register_status`，部署者无法通过 `core状态` 观察绑定与订阅健康度。
