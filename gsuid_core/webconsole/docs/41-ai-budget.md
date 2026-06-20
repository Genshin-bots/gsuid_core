# 41. AI 预算限制 API - /api/ai/budget

按 **Session（群 / 群内成员 / 私聊用户）** 对 AI 的 Token 消耗设置预算上限，支持
**滚动 5 小时 / 天 / 周** 三档窗口、**白名单**突破限制、**主人豁免**，并提供用量排行、
超额预演（干跑）、手动放行等运维接口。

> **设计目标**：前端做一个「按对象配额 + 白名单 + 用量看板」的精细配置页。本组 API
> 在字段与端点上为该 UI 预留了足够的可组合性（列表/详情可选附带实时用量、干跑预演、
> 逐窗口剩余与恢复时间等）。

---

## 41.0 核心概念

### 维度（scope_type）与 Session 语义

GsCore 群聊**全群共享一个 Session 与记忆**（Session ID 不含 user_id），私聊按用户独立。
预算维度据此设计：

| `scope_type` | 含义 | 关键字段 | 典型用途 |
|------|------|---------|---------|
| `global` | 兜底全局总额（对所有会话求和） | 无 | 全局成本闸门/熔断 |
| `group` | 某群**全员共享**额度 | `scope_id`=群号 | 限制某个群的总消耗 |
| `member` | 某群内**某个人** | `scope_id`=群号, `member_id`=用户号 | 限制群里某个话痨 |
| `user` | 某人的**私聊** | `scope_id`=用户号 | 限制某人私聊消耗 |

> **「某个人」如何限制**：群聊里限制某人用 `member` 维度；私聊里限制某人用 `user` 维度。
> 群聊不存在「按人独立」的 Session，因此没有「某人跨所有群的统一额度」这一维度
> （需要的话给每个群各建 `member` 规则，或用 `global` 兜底）。

### 多规则叠加

一条消息可能命中**多条**规则（如 `group` 群额度 + 该群某 `member` 额度 + `global` 总额）。
**任一规则的任一窗口超限即拦截**，返回首个触发的窗口明细。

### 窗口（window）与模式（period_mode）

三档窗口键：`short`（短时，默认 5 小时，逐规则可改 `short_window_hours`）、`day`、`week`。
每档上限 `limit_*`，**0 = 该档不限**。

- `period_mode=rolling`（默认）：滚动窗口，统计「最近 N 小时 / 24h / 7d」。
- `period_mode=fixed`：固定窗口，对齐到 **本地零点 / 周一零点 / epoch 对齐的 N 小时块**，
  到点整体清零（`reset_at` 为精确恢复时间）。

### 计费口径（count_mode，全局）

| 取值 | 记入额度的 Token |
|------|------------------|
| `input_output`（默认） | 输入 + 输出 |
| `total_with_cache` | 输入 + 输出 + 缓存读 + 缓存写 |
| `output_only` | 仅输出 |

### 豁免：白名单与主人

- **白名单**（`AIBudgetWhitelist`）：命中即**永不拦截**。`group_id` 为空=全局豁免（含私聊），
  非空=仅该群内豁免。
- **主人豁免**：开启 `exempt_masters` 后，core 配置 `masters` 永远不受限（等价自动全局白名单）。
- **白名单用量是否计入额度**：由 `count_exempt_usage` 控制，默认 `false`——豁免者的消耗
  **不占用**该会话共享额度（他们突破限制、也不拖累群额度）。

### 已知边界

- **记账点**：仅**交互式**（带事件上下文）的 AI run 计入对应 Session 额度；子 Agent /
  心跳 / 定时任务等后台调用的 Token 只进全局统计、**不占** Session 预算。
- **软上限**：判定发生在调用 LLM **之前**，用量在 run **之后**记账，故最后一次请求可能略微
  超出上限，下一条消息才会被拦（标准的「软封顶」语义）。
- 账本流水仅保留约 8 天（最长只需周窗），每日凌晨自动清理。

---

## 41.1 获取全局配置
```
GET /api/ai/budget/config
```
**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "enable": false,
        "count_mode": "input_output",
        "count_exempt_usage": false,
        "exempt_masters": true,
        "notify_on_block": true,
        "notify_cooldown": 300,
        "block_message": "⚠️ 当前会话的 AI 使用额度已用完（{window}：{used}/{limit} tokens），请稍后再试。"
    }
}
```

---

## 41.2 更新全局配置
```
PUT /api/ai/budget/config
```
部分更新，仅传需要改的字段。`enable` 为总开关（关闭后规则不生效，但**用量仍会记录**，
便于先观察再开启）。`block_message` 支持占位符 `{scope}` `{window}` `{used}` `{limit}` `{reset}`。

**请求体**（示例）：
```json
{ "enable": true, "count_mode": "input_output", "exempt_masters": true, "notify_cooldown": 300 }
```
**响应**：`data` 为更新后的完整配置。`count_mode` 非法或 `notify_cooldown` 为负时返回
`status:1`。

---

## 41.3 规则列表
```
GET /api/ai/budget/rules?scope_type=&enabled=&q=&with_usage=false
```
**Query 参数**：
- `scope_type`：按维度筛选 `global/group/member/user`（可选）
- `enabled`：按启用状态筛选（可选）
- `q`：按名称 / `scope_id` / `member_id` 模糊筛选（可选）
- `with_usage`：为 `true` 时每条规则附带 `usage`（实时逐窗口用量，见 41.5）

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        {
            "id": 1,
            "name": "测试群额度",
            "scope_type": "group",
            "scope_id": "789012",
            "member_id": "",
            "bot_id": "",
            "enabled": true,
            "priority": 0,
            "period_mode": "rolling",
            "short_window_hours": 5,
            "limit_short": 200000,
            "limit_day": 800000,
            "limit_week": 4000000,
            "note": "",
            "created_at": 1718900000,
            "updated_at": 1718900000,
            "usage": { "...": "仅 with_usage=true 时出现，结构见 41.5" }
        }
    ]
}
```

---

## 41.4 创建规则
```
POST /api/ai/budget/rules
```
**请求体**：
```json
{
    "name": "测试群额度",
    "scope_type": "group",
    "scope_id": "789012",
    "member_id": "",
    "bot_id": "",
    "enabled": true,
    "priority": 0,
    "period_mode": "rolling",
    "short_window_hours": 5,
    "limit_short": 200000,
    "limit_day": 800000,
    "limit_week": 4000000,
    "note": "群聊总额度"
}
```
**字段校验**：
- `scope_type` ∈ `global/group/member/user`；`group/member/user` 时 `scope_id` 必填，
  `member` 时 `member_id` 必填。
- `period_mode` ∈ `rolling/fixed`；`short_window_hours` ∈ `1~168`。
- 三个 `limit_*` 至少有一个 `> 0`（否则该规则毫无约束）。
- `name` 留空时自动用维度标签（如「群 789012」）。

**响应**：`data` 为新建规则完整对象。

---

## 41.5 规则详情（含实时用量）
```
GET /api/ai/budget/rules/{rule_id}
```
**响应**（`data.usage` 即逐窗口实时状态）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "id": 1, "name": "测试群额度", "scope_type": "group", "scope_id": "789012",
        "limit_short": 200000, "limit_day": 800000, "limit_week": 4000000,
        "usage": {
            "rule_id": 1,
            "rule_name": "测试群额度",
            "scope_type": "group",
            "scope_label": "群 789012",
            "period_mode": "rolling",
            "blocked": false,
            "windows": [
                { "window": "short", "window_seconds": 18000, "limit": 200000,
                  "used": 35000, "remaining": 165000, "over": false, "reset_at": null },
                { "window": "day", "window_seconds": 86400, "limit": 800000,
                  "used": 120000, "remaining": 680000, "over": false, "reset_at": null },
                { "window": "week", "window_seconds": 604800, "limit": 4000000,
                  "used": 900000, "remaining": 3100000, "over": false, "reset_at": null }
            ]
        }
    }
}
```
> `reset_at` 为窗口预计恢复时间戳（秒）。`fixed` 模式精确；`rolling` 模式按窗口内最早一笔
> 流水估算（窗口内无流水时为 `null`）。

---

## 41.6 更新规则
```
PUT /api/ai/budget/rules/{rule_id}
```
部分更新；未提供的字段沿用原值，并以合并后的最终值做校验。**响应** `data` 为更新后对象。

---

## 41.7 启用/停用规则（快捷开关）
```
POST /api/ai/budget/rules/{rule_id}/toggle
```
**响应**：
```json
{ "status": 0, "msg": "已停用", "data": { "id": 1, "enabled": false } }
```

---

## 41.8 删除规则
```
DELETE /api/ai/budget/rules/{rule_id}
```
**响应**：`{ "status": 0, "msg": "规则已删除", "data": { "id": 1 } }`

---

## 41.9 白名单列表
```
GET /api/ai/budget/whitelist?user_id=&group_id=
```
**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": [
        { "id": 1, "user_id": "10001", "group_id": "789012", "bot_id": "",
          "enabled": true, "note": "群主", "created_at": 1718900000 }
    ]
}
```

---

## 41.10 新增白名单
```
POST /api/ai/budget/whitelist
```
**请求体**：
```json
{ "user_id": "10001", "group_id": "789012", "bot_id": "", "enabled": true, "note": "群主" }
```
- `user_id` 必填。
- `group_id` 为空 = **全局豁免**（含私聊与所有群）；非空 = **仅该群内**豁免该用户。

**响应**：`data` 为新建条目。

---

## 41.11 更新 / 删除白名单
```
PUT    /api/ai/budget/whitelist/{entry_id}
DELETE /api/ai/budget/whitelist/{entry_id}
```
`PUT` 部分更新；`DELETE` 返回 `{ "id": entry_id }`。

---

## 41.12 用量排行（Top 消费者）
```
GET /api/ai/budget/usage?dimension=group&window=day&limit=20&bot_id=&include_exempt=true
```
**Query 参数**：
- `dimension`：聚合维度 `group` / `user` / `member`
- `window`：统计窗口 `short`（默认 5h）/ `day`（24h）/ `week`（7d）
- `limit`：返回条数（1~200，默认 20）
- `bot_id`：按平台过滤（可选）
- `include_exempt`：是否包含豁免用户用量（默认 `true`）

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "dimension": "group",
        "window": "day",
        "since_ts": 1718813600,
        "items": [
            { "group_id": "789012", "total_tokens": 530000 },
            { "group_id": "345678", "total_tokens": 210000 }
        ]
    }
}
```
> `member` 维度的 item 同时含 `group_id` 与 `user_id`；`user` 维度仅含 `user_id`。

---

## 41.13 查看某 scope 的逐窗口用量
```
GET /api/ai/budget/usage/scope?scope_type=group&scope_id=789012&member_id=&bot_id=
```
返回该 scope 下**所有适用规则**的逐窗口 `used/limit/remaining/reset_at`（即 41.5 的
`usage` 结构数组）。`ignore_exempt` 内部恒为真，故即便代表用户是白名单/主人也会照常展示规则
明细。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "scope_type": "group",
        "scope_id": "789012",
        "member_id": "",
        "enabled": true,
        "exempt": false,
        "exempt_reason": "",
        "rules": [ { "rule_id": 1, "scope_label": "群 789012", "blocked": false, "windows": [ "..." ] } ]
    }
}
```

---

## 41.14 干跑预演（诊断「为什么被限」）
```
POST /api/ai/budget/check
```
预演「某用户在某会话发消息」是否会被拦截——**不产生用量、不发消息**，返回完整判定明细。
适合做「这个人为什么被限流」的排障 UX。

**请求体**：
```json
{ "user_id": "10001", "group_id": "789012", "bot_id": "" }
```
> 私聊场景 `group_id` 留空。

**响应**（`data` 即一次 `BudgetDecision`）：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "allowed": false,
        "enabled": true,
        "exempt": false,
        "exempt_reason": "",
        "rule_statuses": [
            { "rule_id": 1, "rule_name": "测试群额度", "scope_type": "group",
              "scope_label": "群 789012", "period_mode": "rolling", "blocked": true,
              "windows": [
                  { "window": "short", "window_seconds": 18000, "limit": 200000,
                    "used": 200500, "remaining": 0, "over": true, "reset_at": 1718905000 }
              ] }
        ],
        "block_rule_id": 1,
        "block_scope_label": "群 789012",
        "block_window": { "window": "short", "limit": 200000, "used": 200500,
                          "remaining": 0, "over": true, "reset_at": 1718905000 },
        "message": "",
        "notify": false
    }
}
```
> `exempt=true` 时 `allowed` 恒为真、`exempt_reason` 为 `master`/`whitelist`。
> 干跑接口的 `message`/`notify` 不填充（那是真实拦截路径才用的字段）。

---

## 41.15 手动放行（清除用量）
```
POST /api/ai/budget/reset
```
清除某 scope 的用量流水，立即放行。`window` 留空 = 清该 scope **全部**流水；指定窗口则只清
该窗口默认时长（5h/24h/7d）内的流水。

**请求体**：
```json
{ "scope_type": "group", "scope_id": "789012", "member_id": "", "bot_id": "", "window": "" }
```
**响应**：`{ "status": 0, "msg": "已清除 N 条用量记录", "data": { "deleted": N } }`

> `global` 维度会清除**所有**流水，请谨慎。

---

## 41.16 看板汇总
```
GET /api/ai/budget/overview
```
一次返回首页卡片所需的汇总数据。

**响应**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {
        "enabled": true,
        "rule_count": 5,
        "enabled_rule_count": 4,
        "whitelist_count": 3,
        "total_tokens_24h": 1830000,
        "blocked_rules": [ { "rule_id": 1, "scope_label": "群 789012", "blocked": true, "windows": [ "..." ] } ],
        "top_groups_24h": [ { "group_id": "789012", "total_tokens": 530000 } ],
        "top_users_24h": [ { "user_id": "10001", "total_tokens": 120000 } ]
    }
}
```

---

## 41.17 前端 UX 建议

- **总开关 + 计费口径**置顶（41.1/41.2）；提示「关闭时仍记录用量」，鼓励先观察后启用。
- **规则表**用 `GET /rules?with_usage=true`（41.3）直接渲染「上限 vs 已用」进度条 +
  `reset_at` 倒计时；行内提供 toggle（41.7）。
- **新建规则向导**：先选维度→填对象→三档上限（可只填一档）→选滚动/固定。维度的语义差异
  （群共享 vs 群内某人 vs 私聊）务必在 UI 上点明。
- **白名单**与规则分区管理；强调「全局豁免 vs 仅某群豁免」。
- **排障入口**：给运营一个输入框（群号 + 用户号）直接打 `POST /check`（41.14）渲染「会不会
  被限 + 命中哪条规则哪个窗口 + 多久恢复」；旁边放「立即放行」按钮调 `POST /reset`（41.15）。
- **看板**用 `GET /overview`（41.16）展示近 24h 总量、当前超限规则、Top 群/用户。
