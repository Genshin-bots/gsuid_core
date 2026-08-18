# 十四、AI 集成：能力代理（AgentNode task-mode）

能力代理是**无人格**的专职执行角色，把「取数 / 写报告 / 出图」与主人格的「角色表达」解耦。
插件注册业务节点（如 `stock_agent` / `stock_report_agent`），主人格经
`create_subagent(agent_profile="…")` 或 Kanban 派活。

> 架构：[`docs/AGENT_NODE_UNIFICATION_20260707.md`](../../../../docs/AGENT_NODE_UNIFICATION_20260707.md)。
> API 速查：[`gscore-ai-core-api` §7.8](../../gscore-ai-core-api/references/07-builtin-tools.md#78-能力代理agentnode-task-mode-节点)。

## 14.1 该用能力代理还是 AI Skill？

| 场景 | 选用 | 原因 |
|------|------|------|
| 多步取数 + **强制工具序** + 结构化交付 | **能力代理** | `tool_names` 白名单 + runner 装配 + prompt 硬门 |
| 可选操作手册 / 脚本配方 | **AI Skill**（`ai_skill`） | 模型需 `list_skills` 自觉加载，难强制工具序 |
| 单次原子查询且已有用户命令 | **`to_ai` 触发器** | 不必再挂节点 |

**写研报 / 专业复盘 / 必须先查插件 API 再 web 补漏** → 一律能力代理，不要只塞 Skill。

## 14.2 核心 API（2026-07 起以 AgentNode 为准）

```python
from gsuid_core.ai_core.agent_node import (
    TASK_BASICS_PACK,
    AgentNode,
    register_agent_node,
    compose_task_prompt,  # 调试用：看 task-mode 最终 system
    resolve_node,
    get_node,
)

register_agent_node(
    AgentNode(
        node_id="my_domain_agent",
        display_name="某域分析代理",
        prompt=MY_PROMPT,                 # 身份核；交付边界由框架叠加
        when_to_use="一句话：何时派给它",
        match_keywords=["域内词1", "域内词2"],
        tool_packs=[TASK_BASICS_PACK],    # artifact/state/record/search/web
        tool_names=["my_plugin_tool_a", "my_plugin_tool_b"],
        boundary_override="",             # 空=默认 DELIVERY_BOUNDARY
        source="plugin",
    )
)
```

| 字段 | 要点 |
|------|------|
| `node_id` | 稳定句柄；主人格应填 **node_id**，勿自造 |
| `prompt` | 纯职能、无角色口癖；**不要**手写「禁止 send_message_by_ai」长段（默认边界会叠） |
| `tool_packs` | 多数业务挂 `task_basics`；**纯渲染节点**可 `[]` 并关掉回填（见 `render_agent`） |
| `tool_names` | 业务工具白名单；trigger 的 `to_ai` 名 = 处理函数名 |
| `match_keywords` | `resolve_node` **按注册序首个命中**；勿与兄弟节点抢词 |
| `boundary_override` | 仅特殊通道（如允许渲染工具发图）时覆写 |

> 旧 `CapabilityAgentProfile` / `register_capability_agent` 仍兼容但废弃；新代码只用 `AgentNode`。
> 预算不在节点上：全局 `task_max_iterations` / `task_max_tokens`。

## 14.3 框架内置节点（7 通用 + 内部评估器）

| node_id | 职责 | 出图？ |
|---------|------|--------|
| `research_agent` | 外部检索 → **事实包**（来源+时点） | 否 → 主人格再派 `render_agent` |
| **`render_agent`** | 已给定事实包 → 美观 HTML/卡片图 | **是**（持有 `render_*`） |
| `code_agent` | 沙盒写代码 / PIL 真文件图 | 脚本产物，非模板 HTML |
| `internal_reporter` | 内部库报告 | 可短 md 图 |
| `memory_curator` | 记忆维护 | 否 |
| `scheduler_assistant` | 定时任务 | 否 |
| `plugin_developer_agent` | 写插件（主人） | 否 |
| `capability_evaluator` | 仅评估用 | — |

**主人格出图主路径（2026-08）**：

```text
create_subagent(agent_profile="render_agent", task=完整事实包+版式要求)
```

- `render_html_to_image` / `render_card` / `render_markdown_to_image` 属 **`media`**，
  由 `render_agent` 白名单持有；交互主人格经 **exclusive 剥离**后不应直调。
- 其它能力代理默认 **禁止** render_*（契约文案）；`render_agent` 例外。

## 14.4 Cookbook：写好一个业务能力代理

### ① 工具优先级硬门（插件数据 > web）

业务域若有结构化 API，prompt **必须**写清顺序，并写进「未调插件工具就交付 = 失败」：

```text
【工具优先级 · 硬门】
1. 先调本插件：xxx_query / yyy_indicators / …
2. web_search_tool / web_fetch_tool 仅补：政策原文、公告细节、工具未覆盖的背景
3. 禁止用网页摘要代替实时报价 / 结构化字段 / 指标值
```

`task_basics` 会带上 `web_search_tool`——**白名单挡不住 web**，靠 prompt + 交付自检。
若节点**绝不该搜网**（如纯渲染），设 `tool_packs=[]` 并在 runner 对 `node_id` 跳过 task 回填
（框架已对 `render_agent` 这样做）。

### ② 数据时效自检（通用，非业务词）

框架 `research_agent` 用的是**通用**表述（数字 / 实时状态 / 时点），**不要**在框架 prompt
里写股票/研报专有名词。业务代理可写域内时点规则，例如：

```text
报价/财务标注工具返回时点或报表期；缺时点或过期须重查或标「时效存疑」。
```

### ③ 交付与出图

| 交付物 | 做法 |
|--------|------|
| 短结论事实包 | 正文 Markdown/JSON；长文 `artifact_put` |
| 群聊长报告 | 业务节点 **只交 MD 事实包**；主人格再 `create_subagent(render_agent)` 出图；禁刷屏念表 |
| 主人格回执 | 短句转述；图已由 render 下发则勿重复 send；勿把 res_ 写进台词 |

**出图主权（2026-08 方案 B）**：非 `render_agent` 的能力代理 **禁止**
`render_*` / `create_subagent` 嵌套 / 插件终局直发出图工具。业务 report 节点
不要再挂 `send_*_report_image`。

### ④ `match_keywords` 拆分「短分析」与「长报告」

同插件可挂两个节点，避免一个 prompt 又要短答又要写 3000 字：

| 节点 | 关键词示例 | 工具差 |
|------|------------|--------|
| `stock_agent` | 股票分析、技术面、估值 | 行情/财务/技术 |
| `stock_report_agent` | **写研报**、研报、深度报告 | 同上（**无**出图工具；出图归主人格→`render_agent`） |

注册时 **report 节点若关键词更特化，仍可能被更早注册的节点抢走**——`resolve_node` 按
**注册表顺序**首个 keyword 命中。把特化节点注册在泛化节点附近，并让泛化节点 **不要**
占用「研报」等词（SayuStock 已把「研报」只留给 `stock_report_agent`）。

### ⑤ 边界与禁止项

- 默认：不调 `send_message_by_ai` / 不角色扮演 / 高风险动作只列「需主人决策」。
- 模拟盘/账本等有专用节点时，研究节点 prompt 写明 **转交**，禁止 `state_*`/`record:stock:*` 自建账。
- **框架代码禁止业务域词**（股价、研报等）；业务词只出现在插件节点与插件工具 docstring。

### ⑥ 最小注册模板（插件内）

```python
# MyPlugin/my_agent/__init__.py
from gsuid_core.ai_core.agent_node import TASK_BASICS_PACK, AgentNode, register_agent_node

_PROMPT = """你是「XX 域分析代理」。无人格，只交付可复核结论。

【工具优先级】
1. 插件工具：my_fetch_a / my_fetch_b
2. web_search 仅补工具没有的背景；禁止用网页代替 my_fetch_* 的结构化字段。

【工作流】规划 <TODO_LIST> → 取数 → 依据写清工具名/字段/时点 → 交付。
【红线】不编造；数据不足列缺口。
"""

def register_my_agents() -> None:
    register_agent_node(
        AgentNode(
            node_id="my_domain_agent",
            display_name="XX 分析代理",
            prompt=_PROMPT,
            when_to_use="需要 XX 域结构化分析时",
            match_keywords=["XX分析", "XX数据"],
            tool_packs=[TASK_BASICS_PACK],
            tool_names=["my_fetch_a", "my_fetch_b", "_get_current_date"],
            source="plugin",
        )
    )

register_my_agents()  # 模块 import 即注册
```

在插件内层 `__init__.py`：`from . import my_agent  # noqa: F401`。

### ⑦ 主人格怎么派

```text
create_subagent(agent_profile="my_domain_agent", task="……含实体与目标……")
create_subagent(agent_profile="stock_report_agent", task="写某某一页纸研报……")
create_subagent(agent_profile="render_agent", task=事实包)  # 通用美观出图
```

`agent_profile` 优先填 **node_id**；自然语言走 `resolve_node`，依赖 keywords。

## 14.5 实例：SayuStock

路径：[`SayuStock/stock_agent/__init__.py`](../../../gsuid_core/plugins/SayuStock/SayuStock/stock_agent/__init__.py)。

| node_id | 用途 |
|---------|------|
| `stock_agent` | 短分析；工具优先硬门；含 financials/indicators/技术分析等 |
| `stock_report_agent` | 完整研报；强制取数清单 + **`artifact_put` Markdown**（出图归主人格→`render_agent`） |
| `papertrade_*` | 模拟盘专用（研究节点禁止越权记账） |

参考其 prompt 写法：**工具优先级表 → 时效 → 交付（事实包 only）→ 红线（模拟盘转交）**。

## 14.6 注册时机与回退

| 场景 | 行为 |
|------|------|
| 插件已注册 | `resolve_node` / 显式 node_id 命中 |
| 未注册 | 常回退 `research_agent`；专业决策应诚实说未挂载插件 |
| 同名 `node_id` | 后写覆盖前写 |

模块级 `register_*()` 即可；Kanban 运行时解析，晚于 `init_planning` 也可。

## 14.7 框架内置工具名（能力代理可挂）

在 `tool_names` 写字符串即可，不必 import。注意日期工具注册名是 **`_get_current_date`**
（不是 `get_current_date`）。

出图相关（**不要**挂在普通业务节点上；终局出图只走 `render_agent`）：

| 工具 | category | 谁持有 |
|------|----------|--------|
| `render_html_to_image` | media | `render_agent` |
| `render_card` / `render_markdown_to_image` | media | `render_agent` / 少量 reporter |
| 插件 `send_*_report_image` | common 等 | **兼容遗留**；业务 report 节点**不要**再挂 |

`task_basics` 已含：`artifact_*`、`state_*`、`record_*`、`search_cognition`、
`web_search_tool`、`web_fetch_tool`——业务节点**不必**再抄一遍。

## 14.8 常见坑

1. **关键词抢路由**：两个节点都写「分析」→ 先注册者永远赢；拆词或改注册序。
2. **白名单漏 trigger 名**：`to_ai` 工具名 = **函数名**（如 `send_technical_analysis`）。
3. **只靠 web**：`task_basics` 自带 search，prompt 必须压优先级。
4. **主人格自渲 HTML**：已改为委派 `render_agent`；插件文档勿再写「主人格 render_html」。
5. **框架混入业务词**：违反通用性；业务词只放插件。
6. **Skill 代替代理**：无法强制工具序；长流程用 AgentNode。
7. **交付边界**：默认禁止直发用户；长文出图一律主人格→`render_agent`，勿让业务节点
   自渲或 `send_message_by_ai` 念长文。

## 14.9 旧 API import（勿用于新代码）

```python
# 废弃兼容
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    register_capability_agent,
)
```

## 十四附、`plugin_developer_agent`

框架元能力：主人让 AI 写插件。工具在 `buildin_tools/plugin_developer.py`，
流程 scaffold → 写码 → validate → 审批安装 → load → `test_plugin_command`。
详见该模块 docstring 与本 SKILL 其它章节（目录 / 触发器 / AGENTS.md）。
