# 十四、AI 集成：能力代理画像（CapabilityAgentProfile）

能力代理画像（CapabilityAgentProfile）是一种**无人格**的专职执行角色，用于将"执行"与"人格表达"解耦。
插件可以注册自己的业务画像（如炒股插件注册 `stock_agent`），让 AI Agent Mesh 在特定领域任务中选择专业能力代理。

> 更多架构细节参见 [`docs/AI_AGENT_ARCHITECTURE.md`](../../../AI_AGENT_ARCHITECTURE.md) §17。

## 14.1 核心概念

| 概念 | 说明 |
|------|------|
| `CapabilityAgentProfile` | 一个 dataclass，描述专职执行角色的职能、提示词与工具集 |
| `register_capability_agent()` | 注册画像到内存注册表（进程级） |
| `resolve_profile()` | 自然语言关键词 → `profile_id`（主人格派任务时用） |
| `_DELIVERY_BOUNDARY` | 共享的交付边界约束段，**必须**拼入画像 prompt |

**内置画像**（框架自带）：`research_agent`（调研）、`code_agent`（编码）、
`internal_reporter`（内部报告）、`memory_curator`（记忆整理）、`scheduler_assistant`（调度辅助）。

**业务画像**（插件注册）：`stock_agent`（股票分析）等——不在框架内置，由插件自行注册。

## 14.2 `CapabilityAgentProfile` 字段说明

```python
from dataclasses import field, dataclass
from typing import List

@dataclass
class CapabilityAgentProfile:
    profile_id: str           # 唯一标识，如 "stock_agent" / "weather_agent"
    display_name: str         # 给用户看的名字，如 "股票研究分析代理"
    when_to_use: str          # 何时该派给它（一句话描述）
    system_prompt: str        # 纯职能 Plan-and-Solve 提示词，绝无人格
    match_keywords: List[str] # 自然语言关键词列表（resolve_profile 匹配用）
    tool_names: List[str] = field(default_factory=list)  # 显式工具白名单（按名挂载）
    tool_query: str = ""      # 可选：再做一次向量检索补充工具的查询词
    max_iterations: int = 20  # 最大迭代次数
    max_tokens: int = 35000   # 最大 token 数
```

## 14.3 注册业务画像的完整示例

以金融场景为例（其它领域如健康打卡 / 学习计划 / 销售追踪同构，把工具换成相应的业务工具即可）：

```python
# MyPlugin/myplugin_agent/__init__.py
"""
MyPlugin 业务能力代理注册模块。

该模块在导入时注册 my_agent，用于让 AI Agent Mesh 在特定业务任务中
选择 MyPlugin 的专业能力代理。
"""

from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    register_capability_agent,
)
from gsuid_core.ai_core.capability_agents.profiles import _DELIVERY_BOUNDARY

# ── 画像 prompt ──────────────────────────────────────────────────────
# ⚠️ 必须拼入 _DELIVERY_BOUNDARY，否则画像会绕过主人格直接给用户发消息
MY_AGENT_PROMPT = (
    """你是一个严谨的「XX 分析代理」。你没有任何角色人格，
只对任务结果负责，不做角色扮演、不加语气词。

【能力边界】
1. 擅长对 XX 领域进行专业分析。
2. 可使用以下工具获取数据并分析。

【工作流】
1. 规划：先输出 <TODO_LIST>，把任务拆成 2~5 步。
2. 执行：依次调用工具完成每一步。
3. 决策必须基于工具数据：每个判断都要回答清楚"来自哪个工具 / 哪个字段"。
4. 如果工具数据不足，不得编造数据；应明确列出缺口，并给出保守结论。
5. 高风险动作一律不自己执行，在交付摘要里显式列出"需要主人决策的动作"。

【交付格式】
① 结论 / 操作建议（简洁可执行）；
② 数据依据：逐条列理由 + 数据来源；
③ 风险提示。
"""
    + _DELIVERY_BOUNDARY   # ← 必须拼接
)


def register_my_agent() -> None:
    """注册 MyPlugin 业务能力代理。"""
    register_capability_agent(
        CapabilityAgentProfile(
            profile_id="my_agent",                # 唯一标识
            display_name="XX 分析代理",            # 给用户看的名字
            when_to_use="需要分析 XX 领域数据的任务",
            system_prompt=MY_AGENT_PROMPT,
            match_keywords=[                      # 自然语言匹配关键词
                "XX分析",
                "XX数据",
                "XX报告",
            ],
            tool_names=[                          # 显式工具白名单
                "my_query_data",
                "my_search_item",
                "my_get_chart",
            ],
            tool_query="",                        # 可选：向量检索补充
            max_iterations=25,
            max_tokens=40000,
        )
    )


# 模块导入时立即注册
register_my_agent()
```

## 14.4 画像 prompt 写作要点（硬约束）

为防止业务画像形式到位但忽略关键约束，prompt 中**必须**包含以下内容：

### ① `_DELIVERY_BOUNDARY` 必须拼入

```python
from gsuid_core.ai_core.capability_agents.profiles import _DELIVERY_BOUNDARY

MY_PROMPT = "..." + _DELIVERY_BOUNDARY
```

从 [`profiles._DELIVERY_BOUNDARY`](../../../gsuid_core/ai_core/capability_agents/profiles.py:58) 直接导入。
**否则**画像会自己调 `send_message_by_ai` 给主人发消息，绕过主人格转译，导致语气和上下文断裂。

### ② 持久化必须用 `record_*`

prompt 中应**显式禁止**回退到 `state_set` / `state_list`：

```
禁止用 state_set / state_list 维护账户/持仓/流水类多条结构化数据；
必须用 record_put / record_append / record_update 把每个集合按
<业务前缀>:<集合名>_<owner> 维护。
如果 record_get 取不到，就新建而不是回退去翻 state_*。
```

> **原因**：`state_list` 会看到旧任务遗留的状态键，导致跨任务数据污染。
> 详见 [`AI_AGENT_ARCHITECTURE.md`](../../../AI_AGENT_ARCHITECTURE.md) §17.1。

### ③ 跨次状态读写顺序

每次开火（周期模板克隆实例）的子任务都是独立的；要读上次状态用 `record_get`，
要写新流水用 `record_append`，要改主表用 `record_update`——三件套语义不要混用。

### ④ 不要假设画像有 evaluate / scheduler 工具

业务画像默认不持有 `evaluate_agent_mesh_capability` / `register_kanban_task`——
那些是主人格层的工具。业务画像只在 Kanban 派出的子任务里跑，**不要**在 prompt 里写
"如果需要更多步骤请自己开 Kanban 任务"。

### ⑤ 诚实底线

业务专业域里如果发现框架未挂载关键外接工具（如插件本身被禁用了某个 API），
必须在交付摘要里明说"我做不到这步"，**不要**靠 `web_search` 拼凑结果。

## 14.5 注册时机与回退机制

| 场景 | 行为 |
|------|------|
| 插件正确注册了画像 | `agent_profile="XX"` → `resolve_profile` 匹配 `match_keywords` → 使用插件画像 |
| 插件未注册画像 | `agent_profile="XX"` → `resolve_profile` 回退到 `research_agent`，评估器 + 主人格会拒绝给出专业决策并提示"框架未挂载对应插件" |
| 同名 `profile_id` 覆盖 | 后注册的同名画像会覆盖先注册的（可用于插件升级或用户自定义） |

**注册时机**：在子模块 `__init__.py` 的模块级代码中直接调用 `register_my_agent()`——
模块导入时即注册。画像只在 `kanban_executor._run_one_task_node` 运行时查询，
所以即使注册晚于 `init_planning` 也没问题。

## 14.6 实际案例：SayuStock 的 `stock_agent`

参照 [`SayuStock/stock_agent/__init__.py`](../../../gsuid_core/plugins/SayuStock/SayuStock/stock_agent/__init__.py:1) 的实现：

```python
# SayuStock/stock_agent/__init__.py（简化）
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    register_capability_agent,
)
from gsuid_core.ai_core.capability_agents.profiles import _DELIVERY_BOUNDARY

STOCK_AGENT_PROMPT = (
    """你是一个严谨的「股票研究分析代理」...
【能力边界】...
【工作流】...
【分析要求】...
【交付格式】..."""
    + _DELIVERY_BOUNDARY
)


def register_stock_agent() -> None:
    register_capability_agent(
        CapabilityAgentProfile(
            profile_id="stock_agent",
            display_name="股票研究分析代理",
            when_to_use="需要分析个股、宽基指数、宏观环境、量价关系、技术面指标的股票研究任务",
            system_prompt=STOCK_AGENT_PROMPT,
            match_keywords=[
                "股票分析", "个股分析", "宏观环境", "宽基", "指数",
                "量价关系", "技术面", "价值面", "基本面", "估值",
                "PB", "PS", "PE", "复盘", "研报",
            ],
            tool_names=[
                "send_stock_info", "send_my_stock", "send_my_stock_img",
                "send_stock_PB_info", "search_stock", "get_stock_change_rate",
                "get_vix_index", "send_cloudmap_img", "get_latest_news",
                "get_crypto_prices",
            ],
            tool_query="",
            max_iterations=25,
            max_tokens=40000,
        )
    )


register_stock_agent()  # 模块导入时立即注册
```

## 14.7 常用 import 速查

```python
# 能力代理画像
from gsuid_core.ai_core.capability_agents import (
    CapabilityAgentProfile,
    register_capability_agent,
    unregister_capability_agent,
    get_profile,
    list_profiles,
    resolve_profile,
)
from gsuid_core.ai_core.capability_agents.profiles import _DELIVERY_BOUNDARY
```

## 14.8 使用框架内置工具（buildin_tools）

插件在注册能力代理画像时，`tool_names` 白名单除了填写插件自身注册的 `@ai_tools` 工具外，
还可以直接引用框架内置（`buildin_tools`）注册的工具名称。框架在实例化能力代理时会从全局
`_TOOL_REGISTRY` 中按名查找并挂载，无需额外导入。

> **区分**：插件业务代码中如果需要直接 *调用* 内置工具（如在启动钩子里手动触发），才需要
> `from gsuid_core.ai_core.buildin_tools import xxx`；能力代理画像只需在 `tool_names` 列表
> 里写工具名字符串即可。

**当前可用的内置工具清单**（以 `_TOOL_REGISTRY` 实际注册为准）：

| 工具名 | 来源模块 | 说明 |
|--------|----------|------|
| `search_knowledge` | `rag_search` | 向量检索知识库，支持类别/插件筛选 |
| `search_image` | `rag_search` | 图片资源向量检索 |
| `web_search_tool` | `web_search` | 基于 Tavily 的 web 搜索 |
| `web_fetch_tool` | `web_fetch` | 抓取网页内容并转为 Markdown |
| `query_user_memory` | `database_query` | 查询用户多群组记忆 |
| `query_user_favorability` | `database_query` | 查询好感度 |
| `update_user_favorability` | `favorability_manager` | 增量更新好感度 |
| `set_user_favorability` | `favorability_manager` | 绝对值设置好感度 |
| `send_message_by_ai` | `message_sender` | 主动以当前人格口吻发消息（**仅主人格可用**） |
| `get_self_info` | `self_info` | 获取完整自我认知（身份/能力/主人） |
| `get_self_persona_info` | `self_info` | 查看 Persona 资源（立绘/头像/音频/配置） |
| `update_self_note` | `self_info` | 写 self_note |
| `get_current_date` | `get_time` | 获取当前日期时间 |
| `create_subagent` | `subagent` | 派生子 Agent 完成即时多步任务 |
| `read_file_content` | `file_manager` | 读取沙盒文件内容 |
| `write_file_content` | `file_manager` | 写入沙盒文件内容 |
| `diff_file_content` | `file_manager` | 对比两个文件差异 |
| `list_directory` | `file_manager` | 列出沙盒目录内容 |
| `execute_file` | `file_manager` | 执行脚本文件（.py/.bat/.sh 等） |
| `execute_shell_command` | `command_executor` | 执行系统 shell 命令（需权限校验） |
| `move_file` | `file_operations` | 在 artifacts 路径内移动文件（不可覆盖） |
| `copy_file` | `file_operations` | 在 artifacts 路径内复制文件 |
| `pack_to_zip` | `file_operations` | 将文件/目录打包为 zip 压缩包 |
| `render_html_to_image` | `html_render_tools` | HTML 模板渲染为图片 |
| `render_markdown_to_image` | `html_render_tools` | Markdown 渲染为图片 |
| `send_meme` | `meme_tools` | 发送表情包 |
| `collect_meme` | `meme_tools` | 收藏表情包 |
| `search_meme` | `meme_tools` | 搜索表情包 |
| `add_once_task` | `scheduler` | 注册一次性定时任务 |
| `add_interval_task` | `scheduler` | 注册周期定时任务 |
| `list_scheduled_tasks` | `scheduler` | 列出定时任务 |
| `query_scheduled_task` | `scheduler` | 查询定时任务详情 |
| `modify_scheduled_task` | `scheduler` | 修改定时任务 |
| `cancel_scheduled_task` | `scheduler` | 取消定时任务 |
| `pause_scheduled_task` | `scheduler` | 暂停定时任务 |
| `resume_scheduled_task` | `scheduler` | 恢复定时任务 |
| `state_get` / `state_set` / `state_delete` / `state_list` / `state_append` | `state_store` | 通用持久键值状态 |
| `record_put` / `record_get` / `record_list` / `record_append` / `record_update` / `record_delete` / `record_summary` | `state_store` | 通用结构化集合 |
| `register_kanban_task` | `kanban_tools` | 创建 Kanban 任务树 |
| `respawn_subtask` / `fail_task_tree` / `respond_subtask_approval` | `kanban_tools` | 任务树重派/终结/审批 |
| `artifact_put` / `artifact_get` / `artifact_list` / `artifact_get_recent` | `kanban_tools` | 任务节点 artifact 增查 |
| `evaluate_agent_mesh_capability` | `kanban_tools` | Kanban 任务树前置评估 |
| `discover_tools` / `list_available_tools` | `dynamic_tool_discovery` | 动态工具发现（按需搜索新工具） |

> **提示**：能力代理默认还会被框架无条件追加一批"永远工具"（`_ALWAYS_TOOLS`），包括
> `artifact_*`、`state_*`、`search_knowledge`、`web_search_tool`、`web_fetch_tool` 等基础能力，
> 即使画像 `tool_names` 忘写也不会丢失。详见 [`buildin_tools/__init__.py`](../../../gsuid_core/ai_core/buildin_tools/__init__.py:95) §三。

---

## 十四附、框架自带的插件开发代理（`plugin_developer_agent`）

除了让插件**注册自己的**业务画像（§14），框架还内置了一个**元能力**画像
`plugin_developer_agent`（display_name「插件开发代理」）——它让 AI 能**端到端编写一个
本框架插件并自助热加载使用**，无需人工拷文件、重启进程。

### 14附.1 定位与触发

| 项 | 内容 |
|----|------|
| profile_id | `plugin_developer_agent` |
| 触发关键词 | 「写插件 / 开发插件 / 做个插件 / 生成插件 / 插件开发 / 帮我写插件 …」 |
| 权限 | **仅主人（PM=0）**——每个写/删/加载工具都 `check_func=check_pm` |
| 落盘 | 直接写入 `gsuid_core/plugins/<Name>/`（路径强制限定单插件目录，防穿越） |
| 加载 | 复用框架 `reload_plugin()` 热加载，命令立即生效 |

主人对 AI 说「帮我写一个 XX 插件」→ 主人格经 `resolve_profile` 命中本画像 →
经 `create_subagent(agent_profile="写插件")` / Kanban 派活 → 代理执行。

### 14附.2 专用工具集（`category="plugin_dev"`）

实现于 [`buildin_tools/plugin_developer.py`](../../../gsuid_core/ai_core/buildin_tools/plugin_developer.py:1)。
插件代码全程在**工作区**用 file_manager 的 `read_file_content` / `write_file_content` 读写，
本组工具只负责脚手架、自检、审批安装与热加载：

| 工具 | 作用 |
|------|------|
| `scaffold_plugin` | 在工作区生成嵌套加载骨架（外层包 + 内层同名包 + Plugins 声明 + 业务示例） |
| `validate_plugin` | 对工作区里插件所有 `.py` 做 `py_compile` 语法自检 |
| `copy_to_plugin_dir` | 非阻塞发起安装审批，主人同意后把工作区插件装进 `plugins/`（覆盖已存在的同名插件时分两步、各一次审批：先以临时名安装新代码、再单独审批删除旧目录，全程不直接删同名目录；运行期 `data/` 由插件自身负责兼容，不做特殊保留） |
| `load_plugin_into_core` | 热加载（全新 / 改动后均走它），原样回传 `reload_plugin` 的成功/失败文本 |
| `test_plugin_command` | **功能自测**：实跑某条命令（MockBot 拦截下发、只回收产出），交付前确认命令真能跑出预期结果 |
| `read_plugin_dev_guide` | 按需查阅本 SKILL 全文（空参看目录、传章节标题读正文） |

### 14附.3 端到端工作流

代理 system_prompt 内置了本 SKILL 的精要 + 红线，并强制按此顺序闭环：

```
规划 <TODO_LIST>（不确定就 read_plugin_dev_guide 查证）
  → scaffold_plugin 起骨架
  → write_file_content 逐文件写业务代码（面向用户的命令按需写 to_ai）
  → validate_plugin 语法自检（不过就改）
  → copy_to_plugin_dir 发起安装审批（返回「已发起审批…请停止」即原样交回并结束本轮；主人同意后框架重新调度，重入再调它才落 plugins/。**覆盖已存在的同名插件**时会再要一次「删除旧目录」审批，照样按返回提示停手等待重新调度即可）
  → load_plugin_into_core 热加载（含 ❌ 则读报错→改→重载，直到「✨ 已重载插件」）
  → test_plugin_command 功能自测：实跑每条核心命令、核对产出（不符就改→重载→再测）
  → 交付：插件名 + 命令清单 + **自测结果** + 文件清单（交回主人格转告主人）
```

> **「测了再回复」是硬要求**：语法过、能加载 ≠ 功能正确。交付前必须用
> `test_plugin_command` 实跑核心命令拿到符合预期的产出；测不了的命令（没写 to_ai
> 或属写入/不可逆副作用）如实标注"需主人手动验证"，绝不假装测过。

> 这就是「AI 自助写插件」的元能力：它产出的插件本身仍然遵循本 SKILL 的全部规范
> （目录结构、触发器签名、`convert_img`、`gs_subscribe` 推送、LLM.md 代码红线等）。
