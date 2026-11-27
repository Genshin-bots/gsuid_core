---
name: gscore-plugin-review
description: >
  当用户要求"审核 / review 一个 GsCore 插件"、"这个插件适合放进商店吗"、
  "帮我评估插件能不能上架插件商店"、"检查这个插件有没有安全/隐私/兼容问题"、
  "按 GsCore 插件开发规范审查仓库"、"给插件做上架前体检"、
  "排查插件的数据库/配置/Web路由/AI工具/资源路径/代码红线"时触发此 SKILL。
  凡是对第三方或自研 GsCore 插件做代码审查、商店准入评估或合规体检的任务，都应优先读取此 SKILL。

  面向 GsCore（早柚核心 / gsuid-core）插件商店维护者与插件作者的**插件审核指南**。
  以实审 `CalabiyauUID` 等真实插件的经验为蓝本，覆盖插件审核全流程：
  静态扫描脚本与报告模板、目录结构与 Python 3.11+ 兼容性、触发器/消息/渲染/帮助/状态、
  数据存储/运行时资源/缓存与备份路径、数据库/配置/定时任务/订阅与热重载、
  Web 路由/插件前端页/安全与隐私红线、AI 工具/知识库/别名/能力代理跨插件隔离、
  代码红线/类型检查/全链路异步，以及 `CalabiyauUID` 完整审核实战复盘。
  与 `gscore-plugin-development` 的区别：后者教「怎么从零写插件」，本 SKILL 教
  「怎么系统审查一个已有插件并给出 P0/P1/P2 分级与商店准入结论」。
---

# GsCore 插件审核与商店准入指南（核心入口）

> 本 SKILL 按审核维度拆分为「主入口 + `references/` 子文档」。审核插件时先按本页的
> **标准流程**做静态体检，再根据目标插件实际用到的子系统（如 Web 路由、AI 能力代理、
> 数据库、外置资源仓）按需读取对应章节，**不要**一次把全部子文档塞进上下文。

## 与其他 SKILL 的分工

| 你的任务 | 该读的 SKILL |
|----------|-------------|
| **审核已有插件 / 评估能否上架商店 / 输出分级审查报告** | **本 SKILL** |
| 从零编写或改造插件功能 | [`gscore-plugin-development`](../gscore-plugin-development/SKILL.md) |
| 核对 AI Core 对外 API 精确签名 | [`gscore-ai-core-api`](../gscore-ai-core-api/SKILL.md) |
| 核对框架内部行为（加载器 / 热重载 / 独占工具剥离 / 数据库闸门） | [`gscore-development`](../gscore-development/SKILL.md) |
| 改写审核结论、README 或商店提示文案 | [`human-talk`](../human-talk/SKILL.md) |

## 文档目录索引

| 章节 | 审核范围 | 链接 |
|------|---------|------|
| 一 | 审核流程、静态扫描脚本与报告模板（隔离克隆、AST 红线扫描、导入/ruff/basedpyright 实测、结论模板） | [references/01-workflow-and-static-checks.md](./references/01-workflow-and-static-checks.md) |
| 二 | 目录结构、打包依赖与兼容性（嵌套入口、meta 插件、`pyproject.toml`、`requires-python` 3.11 陷阱、体积与 License） | [references/02-structure-packaging-and-compat.md](./references/02-structure-packaging-and-compat.md) |
| 三 | 触发器、消息收发、渲染、帮助与状态（前缀隔离、权限、`on_meta`、PIL/pytakumi/playwright、`register_help`/`register_status`、文档核对） | [references/03-triggers-messaging-and-help.md](./references/03-triggers-messaging-and-help.md) |
| 四 | 数据存储、运行时资源、缓存与备份（`get_res_path` 红线、禁向源码目录写盘/clone、内存与磁盘上限、`register_backup_path`、镜像容灾） | [references/04-data-storage-and-resources.md](./references/04-data-storage-and-resources.md) |
| 五 | 数据库、配置管理、定时任务、订阅与热重载（SQLModel 基类、读写分离、写闸门 10s、`exec_list`、`StringConfig` 脱敏、`gs_subscribe`、调度与停机钩子） | [references/05-database-config-and-scheduler.md](./references/05-database-config-and-scheduler.md) |
| 六 | Web 路由、插件前端页、隐私与安全审计（共享 `app`/`PluginAPI`、鉴权、禁泄露公网 IP 与明文 HTTP、防群聊劫持、频控、SSRF/路径穿越/XSS、凭据与免责声明） | [references/06-web-security-and-privacy.md](./references/06-web-security-and-privacy.md) |
| 七 | AI 命令、工具、知识库、别名、Skill 与能力代理（`to_ai` vs `@ai_tools`、召回面、`ai_return`、`background=True`、`scope` 隔离、`AgentNode` 防抢词与 `shared_tool_names`） | [references/07-ai-integration-review.md](./references/07-ai-integration-review.md) |
| 八 | 代码红线、类型安全与异步 I/O 审计（`Any`/`cast`/`type: ignore`/`getattr`/兜底 `try-except`、`basedpyright` 真 bug 识别、同步客户端假异步、`Bot`/`_Bot`/`MockBot`） | [references/08-code-redlines-and-async.md](./references/08-code-redlines-and-async.md) |
| 九 | 完整实战案例：`CalabiyauUID` 商店准入审核复盘（从命令扫描到 P0/P1/P2 拆解与最终定级） | [references/09-case-study-calabiyau.md](./references/09-case-study-calabiyau.md) |

## 标准审核流程（6 步）

1. **隔离获取源码**：将目标仓库浅克隆到临时 `scratch/` 目录，绝不直接克隆到当前 Core 生产 `plugins/` 目录，先记录 commit 短号、文件数、体积与扩展名分布。见 [§1.1](./references/01-workflow-and-static-checks.md#11-准备与隔离获取)。
2. **跑自动化静态体检**：一次性执行 AST 红线扫描、`gsuid_core.*` 导入实测、第三方依赖核对、外链域名清单、`ruff check` 与 `basedpyright`。见 [§1.2](./references/01-workflow-and-static-checks.md#12-自动化静态扫描脚本)。
3. **核对基础面与兼容性**：检查 `pyproject.toml`（依赖与 Python 3.11 兼容）、入口三件套、`Plugins` 前缀、资源目录路径与 `README`/`help.json` 一致性。见 [§二](./references/02-structure-packaging-and-compat.md)、[§三](./references/03-triggers-messaging-and-help.md)、[§四](./references/04-data-storage-and-resources.md)。
4. **深审高风险子系统**：只要插件包含以下任一能力，必须逐行读源码，不得只看扫描汇总：
   - **Web 路由 / 登录页 / 前端页 / 文件下载** → 必读 [§六](./references/06-web-security-and-privacy.md)
   - **数据库表 / 迁移 / 配置项 / 定时推送** → 必读 [§五](./references/05-database-config-and-scheduler.md)
   - **AI 工具 / 知识库 / 能力代理 / Skill** → 必读 [§七](./references/07-ai-integration-review.md)
5. **以框架源码为事实源交叉验证**：对疑似违例（例如 `bot.send(bytes)`、热重载清理、工具独占剥离）必须查 `gsuid_core/` 源码确认真实行为，**禁止凭记忆误报**。
6. **按分级标准出具审核报告**：按 P0 / P1 / P2 列出精确文件与行号、触发条件、实际影响与可执行修改方案，并给出明确的商店准入结论。见 [§1.4](./references/01-workflow-and-static-checks.md#14-审核报告输出模板)。

## 严重级别与商店准入标准

| 级别 | 含义 | 商店准入决定 | 典型示例 |
|------|------|--------------|---------|
| **🔴 P0（阻断项）** | 安全/隐私漏洞、恶意行为、直接崩溃或破坏宿主/其他插件运行 | **禁止直接上架**，必须修复复审 | 泄露宿主机公网 IP、群聊暴露可劫持登录链接、无鉴权写接口、Python 3.11 直接崩溃、向插件源码目录写盘/clone、AI 代理泛词抢占其他插件、无前缀 `on_keyword`/`on_message` 污染全局消息流、硬编码密钥或恶意执行 |
| **🟡 P1（建议修）** | 潜在运行时异常、性能/内存隐患、规范偏离、文档与命令不一致 | **强烈建议修复后上架**（可附条件） | `basedpyright` 暴露的 `NotRequired` 键 `KeyError`、同步 HTTP 假异步、长耗时 `to_ai` 缺 `background=True`、无界全局字典/缓存无淘汰、README 命令与实际触发器不符、缺 `register_status` |
| **🟠 P2（合规与提示）** | 外部服务稳定性、运营告知、可维护性提升项 | **允许上架**，须在 README 或 `installMsg` 写明 | 强依赖第三方/个人域名接口、逆向抓包签名维护风险、账号凭证本地存储免责声明、最低 Core 版本号核准、缺少离线单元测试 |

## 核心审核红线速记（高发问题）

- **不写源码目录**：运行时数据、缓存、下载素材与外部资源仓一律落 `get_res_path("<PluginName>")`（`data/<PluginName>/`），禁止写进 `plugins/<PluginName>/` 或 `Path.cwd()`。
- **不漏 Python 3.11 兼容**：Core 支持 `>=3.11,<4.0` 且依赖安装器**不校验** `requires-python`；排查 `shutil.rmtree(..., onexc=...)`、`type X = ...`、3.12 专属标准库 API。
- **不泄宿主机 IP、不发群聊明文登录链接**：Web 登录页不得在未配域名时自动查外网 IP 并广播 `http://<公网IP>:8765/...`；个人账号登录命令应限私聊，发码/登录接口须有服务端频控。
- **不破坏其他插件生态**：插件须配 `force_prefix`；`AgentNode.match_keywords` 禁止写 `"复盘"` / `"战绩分析"` / `"查询"` 等无域泛词；被主人格与代理共用的只读工具须显式加入 `shared_tool_names`，否则会被框架独占剥离。
- **不绕开框架基础设施**：主动推送一律走 `gs_subscribe`（禁遍历 `gss.active_bot`）；Web 路由复用 `gsuid_core.webconsole.app_app.app` 或 `PluginAPI`（禁另起 `FastAPI`/`uvicorn`）；数据库不写 `__tablename__` 且纯查询挂 `@with_read_session`。
- **不凭猜测定罪**：每条 P0/P1 必须附精确文件、行号与复现/触发链；涉及框架行为时须先核对 `gsuid_core/` 源码（如 `reload_plugin.py` 是否已自动清理定时任务与路由、`bot.send(bytes)` 是否已内部处理图片转换）。

## 关联文档

- 插件开发规范：[`.agents/skills/gscore-plugin-development/SKILL.md`](../gscore-plugin-development/SKILL.md)
- AI Core 插件 API：[`.agents/skills/gscore-ai-core-api/SKILL.md`](../gscore-ai-core-api/SKILL.md)
- 框架内部实现与已知坑：[`.agents/skills/gscore-development/SKILL.md`](../gscore-development/SKILL.md)
- 代码红线总则：[`AGENTS.md`](../../../AGENTS.md)
- 技术写作规范：[`.agents/skills/human-talk/SKILL.md`](../human-talk/SKILL.md)
