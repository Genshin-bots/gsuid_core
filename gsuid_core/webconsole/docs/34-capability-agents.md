# 能力代理节点 API - `/api/ai/capability-agents`

> 后端实现：`gsuid_core/webconsole/capability_agents_api.py`
>
> 数据来源：`gsuid_core/ai_core/agent_node/`（统一注册表）+
> `capability_agents/persistence.py`（用户节点持久化）。

> ⚠️ **v2 破坏性变更（2026-07-07，AgentNode 统一）**：字段 `profile_id`→`node_id`、
> `system_prompt`→`prompt`；`max_iterations`/`max_tokens` 移除（预算统一走 AI 配置
> `task_max_iterations`/`task_max_tokens`）；新增 `tool_packs`/`prompt_style`/
> `boundary_override`；source 增加第四态 `persona`（persona 投影节点，只读）。
> 详见 `docs/AGENT_NODE_UNIFICATION_20260707.md`。下文旧字段描述以该说明为准。

## 概念

「能力代理（Capability Agent）」是**无人格**的专职执行代理。当 AI 主人格在
对话中识别到"这是一个需要多种能力协作的复合任务"时，会通过 `register_kanban_task` /
`create_subagent` 工具把执行委派给某个**画像（Profile）**——本组 API 管理这些画像。

> 画像和人格的区别：人格（Persona）有口吻、好感度、合规性等**社交属性**；画像
> 只是"一段中性提示词 + 一份工具白名单 + 一组匹配关键词"，**只对任务结果负责**。

## 三种来源

| Source | 来源 | 是否可写 |
|--------|------|---------|
| `builtin` | 框架内置（v3 收敛为 5 个：`research_agent` / `code_agent` / `internal_reporter` / `memory_curator` / `scheduler_assistant`） | **只读**，前端不允许改 / 删 |
| `plugin`  | 其他插件用 `register_capability_agent` 注册 | **只读** |
| `user`    | 管理员在 webconsole 上手工新建 | 可 PATCH / DELETE |

落盘路径：`data/ai_core/capability_agents/<profile_id>.json`。框架启动时
（`planning.startup.init_planning` 末尾）由 `load_user_profiles` 挂回内存注册表。

---

## 1. 列表

```
GET /api/ai/capability-agents/list?source=user|builtin|plugin
```

`source` 可选，留空返回全部。

**响应**：

```json
{
  "data": {
    "count": 3,
    "items": [
      {
        "profile_id": "research_agent",
        "display_name": "调研助手",
        "when_to_use": "需要多步调研、资料收集、综合分析的任务",
        "system_prompt": "...",
        "match_keywords": ["调研", "研究", "分析", ...],
        "tool_names": [],
        "tool_query": "",
        "max_iterations": 20,
        "max_tokens": 35000,
        "source": "builtin"
      }
    ]
  }
}
```

---

## 2. 详情

```
GET /api/ai/capability-agents/{profile_id}
```

返回该画像的完整 DTO（同列表 item 形状）。不存在时 `status=1`。

---

## 3. 新建

```
POST /api/ai/capability-agents
```

**请求体**：

```json
{
  "profile_id": "finance_agent",
  "display_name": "操盘助手",
  "when_to_use": "需要查行情、做仓位决策、每日复盘的金融任务",
  "system_prompt": "你是一个严谨的量化操盘代理……",
  "match_keywords": ["炒股", "操盘", "股票", "金融", "行情"],
  "tool_names": ["send_stock_info", "send_my_stock", "get_vix_index"],
  "tool_query": "股票行情 仓位",
  "max_iterations": 25,
  "max_tokens": 40000,
  "base": "research_agent"
}
```

**字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `profile_id` | ✅ | 句柄，正则 `^[a-zA-Z][a-zA-Z0-9_]{0,63}$` |
| `display_name` | ✅ | 给用户看的中文名 |
| `system_prompt` | ✅ | 纯职能 Plan-and-Solve 提示词，**禁止角色化语言** |
| `tool_names` | ✗ | 显式工具白名单（按工具名挂载） |
| `tool_query` | ✗ | 留空且 `tool_names` 非空 → 不做向量补充；为空时按 task 文本补充 |
| `match_keywords` | ✗ | AI 主人格用 `agent_profile="操盘"` 这类自然语言时的解析关键字 |
| `base` | ✗ | 以哪个已存在画像（builtin / plugin / user 都行）为模板复制字段，本请求未填的字段自动 fallback 到 base |

**新建逻辑**：

1. 校验 `profile_id` 命名规范；
2. `profile_id` 已存在 → 返回错误（请改用 PATCH）；
3. 若指定 `base` 且该画像存在，未传 / 为空的字段从 base 复制；
4. 注册到内存 + 落盘 `data/ai_core/capability_agents/<id>.json`。

---

## 4. 编辑

```
PATCH /api/ai/capability-agents/{profile_id}
```

**仅允许编辑 `source="user"` 的画像**。builtin / plugin 画像返回 `status=1` 拒绝。

请求体所有字段可选，未传的保持原值。常用于"先复制 builtin 画像再改一改"工作流：

1. 前端先 POST 新建（带 `base="research_agent"`）；
2. 然后 PATCH 持续微调 `system_prompt` 或 `tool_names`。

---

## 5. 删除

```
DELETE /api/ai/capability-agents/{profile_id}
```

**仅允许删除 `source="user"` 的画像**。同时清掉磁盘文件 + 内存注册表条目。
builtin / plugin 画像返回 `status=1` 拒绝。

---

## 6. 可挂载工具枚举

```
GET /api/ai/capability-agents/_tools/available
```

返回**全量已注册工具**列表（按 `plugin / category / name` 排序），前端做
`tool_names` 多选框时用：

```json
{
  "data": {
    "count": 67,
    "items": [
      {
        "name": "send_stock_info",
        "description": "查询股票实时行情...",
        "category": "default",
        "plugin": "SayuStock"
      },
      ...
    ]
  }
}
```

> 注意：能力代理运行时**还会自动附加** `_ALWAYS_TOOLS`（state_*, task_*,
> search_knowledge, web_search_tool, web_fetch_tool 等基础工具），不需要在 `tool_names`
> 里重复列出。前端展示时可在这些"自动挂载"工具旁加个 🔒 标注。

---

## 7. 前端界面建议

### 7.1 主视图：画像卡片墙

- 三个 Tab：「内置 builtin」「插件 plugin」「我的 user」。
- 每张卡片：`display_name` + `profile_id` + `when_to_use` 摘要 + 工具数量徽标。
- builtin / plugin 卡片右上角显示 🔒 图标，悬浮提示"框架级画像，仅可查看"。
- user 卡片有「编辑 / 删除 / 复制为新画像」三个操作。

### 7.2 新建 / 编辑表单

- 顶部：`profile_id`（新建时可编辑、编辑时只读）+ `display_name`。
- 中部：
  - `system_prompt` 大段 textarea + Markdown 预览；
  - `when_to_use` + `match_keywords` 标签输入框；
  - `tool_names` 多选框（从 `/_tools/available` 拉数据，按 plugin 分组）；
  - `tool_query` 单行；
  - `max_iterations` / `max_tokens` 数字。
- 底部：「保存」+「以此为模板新建」（带 `base=该画像`，跳到新建表单）。

### 7.3 复制模板工作流

主要场景：**插件没注册业务画像时，管理员手工兜底**。例如 SayuStock 还没注册
`finance_agent`，管理员可以：

1. 列表页选 `research_agent`（builtin），点「以此为模板新建」；
2. 在新建表单里：
   - `profile_id` 填 `finance_agent`；
   - 在 `tool_names` 里勾选 `send_stock_info` / `send_my_stock` / `get_vix_index` 等股票工具；
   - 改 `system_prompt`，强调"金融决策必须基于工具数据，不允许只靠 web_search 标题"；
3. 保存——主人格在通过 `evaluate_agent_mesh_capability` / `register_kanban_task`
   拆任务时，可以把 `finance_agent` 直接分配给子任务。
