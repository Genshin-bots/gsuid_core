# 七、AI 命令、工具、知识库、别名、Skill 与能力代理审核

> 返回 [SKILL 主入口](../SKILL.md)

当插件接入 `gsuid_core.ai_core` 时，它不只服务自身命令，还会直接进入全局 AI 工具注册表、向量知识库与能力代理路由。**商店公开插件必须同时保证「自身召回准确」与「不干扰其他插件和主人格」。**

## 7.1 `to_ai` 与 `@ai_tools` 规范核查

对照 [10-ai-to-ai-and-ai-return.md](../../gscore-plugin-development/references/10-ai-to-ai-and-ai-return.md)、[11-ai-tools-decorator.md](../../gscore-plugin-development/references/11-ai-tools-decorator.md) 与 [test_ai_tool_docstrings.py](../../../../tests/test_ai_tool_docstrings.py)：

| 检查项 | 级别 | 审核要点 |
|--------|------|---------|
| `to_ai` 与 `@ai_tools` 二选一 | **P0** | 同一函数**绝对禁止**同时挂 `@sv.on_*(..., to_ai=...)` 和 `@ai_tools`（重复注册或异常） |
| 禁止对高危/交互式命令挂 `to_ai` | **P0** | 管理员写操作、清数据/改配置、多轮 `bot.receive_resp()` 会话（`MockBot` 恒返回 `None`）、`on_file` 文件接收命令均不应挂 `to_ai` |
| Docstring 位置与 `Args:` 完整性 | **P0/P1** | `@ai_tools` 的 docstring 必须在函数体**第一条语句**（写在 `logger.info` 后面会导致 `__doc__ is None`、永远召不回）；`to_ai` 必须写明适用自然语言场景、`Args: text:` 格式及至少两个示例（无参写「无需参数，留空即可」） |
| 跨措辞召回面（`covers` / `aliases`） | P1 | 必须填写 `covers`（数据域与能力覆盖）和**带领域前缀**的 `aliases`（如 `卡拉彼丘·查战绩`）；禁止写无领域前缀的裸别名（如 `"查战绩"`、`"排行榜"`），避免与商店其他游戏插件撞车 |
| `category` 合理分级 | P1 | 仅高频核心纯文本工具放 `category="common"`，低频/子代理辅助工具放 `category="default"`；**严禁**插件越权把业务工具放进框架保底分类 `self` / `buildin`（无条件塞进每轮主会话浪费 token） |
| 数据层 `ai_return` 注入 | P1 | 出图触发器应在「数据已拿到、图片未生成」的数据层调用 `ai_return()`（或封装的惰性函数），输出关键数值与错误原因；`_ai_return_xxx` 允许 `try/except` 防影响出图 |
| 长耗时命令 `background=True` | P1 | 常规耗时 > 15–30 秒或依赖外部队列轮询/大文件下载的 `to_ai` / `@ai_tools` 工具，应声明 `background=True`（配合理 `timeout` 与默认 `background_grace=5.0`），避免 AI 调用时锁死群聊会话或超 60s 冻结 |

## 7.2 知识库（`ai_entity`）与别名（`ai_alias`）隔离

对照 [12-ai-knowledge-and-alias.md](../../gscore-plugin-development/references/12-ai-knowledge-and-alias.md)：

1. **别名必须带 `scope` 隔离（P0/P1）**：
   - `ai_alias(formal, aliases, scope="<PluginName>")` 必须传 `scope`（如 `scope="CalabiyauUID"`），防止不同游戏的角色外号/模式简称（如「排位」「生化」「将军」「牢大」）跨游戏串味。
2. **知识条目 `id` / `plugin` / `entity` 规范（P1）**：
   - `id` 全局唯一且带插件前缀（如 `calabiyau_role_146_profile`）；
   - `plugin` 字段等于插件名（供卸载/重载时 `unregister_entities_of_plugin` 精确清理）；
   - 有单一主语（具体角色/武器）的条目填写 `entity`，总表/指南类条目**不要**填 `entity`；
   - 若从外部 JSON 索引动态加载知识文件，必须检查相对路径防穿越（`resolved` 落在知识目录内）。
3. **AI 开关热启兼容（P1）**：
   - 若插件在 `calabiyau_ai/__init__.py` 顶层写 `if ai_config.get_config("enable").data: register_...()`，用户先启动 Core 后在 WebConsole 动态打开 AI 开关而不重启时，该插件的 AI 工具和能力代理会缺失；推荐评估轻量注册或在文档说明开启 AI 后需重载插件。

## 7.3 能力代理（`AgentNode`）跨插件防线（高发 P0）

对照 [14-ai-capability-profile.md](../../gscore-plugin-development/references/14-ai-capability-profile.md) 及框架实现 [registry.py](../../../../gsuid_core/ai_core/agent_node/registry.py)、[support.py](../../../../gsuid_core/ai_core/agent_run/support.py) 核查：

| 检查项 | 级别 | 核查规则与框架源码依据 |
|--------|------|----------------------|
| **禁用废弃 API** | P1 | 新插件应使用 `AgentNode` + `register_agent_node`，不再使用已废弃的 `CapabilityAgentProfile` / `register_capability_agent` |
| **严禁泛词 `match_keywords` 抢路由** | **P0** | 框架 `match_capability_node()` 按显式 `match_keywords` 子串做最长匹配。**禁止**写入无域泛词（如 `"复盘"`、`"战绩分析"`、`"上分建议"`、`"数据分析"`、`"查询"`）；必须全部带插件/游戏域限定（如 `"卡拉彼丘"`、`"卡丘复盘"`、`"超弦体"`） |
| **核对 `shared_tool_names` 防误剥离** | **P0/P1** | 框架 `agent_run/support.py::_capability_exclusive_tool_names()` 会把节点 `tool_names` 中**未列入 `shared_tool_names`** 的业务工具从主人格工具池**独占剥离**。若节点挂载了日常单步查询工具（如战绩卡片、活动列表、排行榜），却未同时写进 `shared_tool_names`，会导致主人格连简单查询都要绕路 `create_subagent` |
| **遵守出图主权** | P1 | 业务分析代理只交付结构化事实包，**不得**把 `render_html_to_image` / `render_card` / `render_markdown_to_image` 写进业务节点 `tool_names`（终局出图由主人格派 `render_agent`）；`prompt` 不手写冗长禁止发消息套话（默认交付边界由框架叠加） |
| **工具优先级硬门与无编造约束** | P1 | 若节点含 `TASK_BASICS_PACK`（自带 `web_search_tool`），prompt 必须写明「优先调插件结构化工具，禁止用网页摘要替代玩家实时数值，未调工具不编造数字」 |
| **人格中立** | P1 | 能力代理 `prompt` 必须无人格、无角色口癖，不违反 `AGENTS.md §1.9` |

**真实案例（`CalabiyauUID`）**：
- 文件：[analyst_agent.py#L32-L54](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/calabiyau_ai/agents/analyst_agent.py#L32-L54)
- **正向**：使用新版 `AgentNode` + `TASK_BASICS_PACK`、无角色口癖、写了「插件工具优先于 web_search / 注明快照时间 / 不编造数值」硬门、未越权挂 `render_*`。
- **P0 缺陷**：
  1. `match_keywords` 含泛词 `"战绩分析"`、`"复盘"`、`"上分建议"`，与其他游戏或股票插件共存时会抢占路由；
  2. `tool_names` 挂了 10 个核心工具（含 `calabiyau_stats_summary`、`calabiyau_event_timeline` 等），但未填 `shared_tool_names`，导致这 10 个工具全部被框架从主人格独占剥离。

## 7.4 运行时 AI Skill（`ai_skill`）审查

若插件通过 `ai_skill(Path(__file__).parent / "skills")` 注册了随包分发的运行时 Skill（[21-ai-skill-registration.md](../../gscore-plugin-development/references/21-ai-skill-registration.md)）：
- 确认目录是业务运行时 Skill（`skills/<name>/SKILL.md`），不是把开发文档 `.agents/skills` 误注册进去；
- 审查附带 `scripts/` 下的全部脚本（Python/Shell）：无外发凭据、无任意写系统路径、无危险 shell 拼接（P0）。
