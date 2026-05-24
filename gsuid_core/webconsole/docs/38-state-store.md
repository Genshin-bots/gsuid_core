# 持久化状态浏览器 API - `/api/ai/state-store`

> 后端实现：`gsuid_core/webconsole/state_store_api.py`（2026-05-25 落地）
>
> 表：`AIPersistentState`（`gsuid_core/ai_core/state_store/models.py`）。
>
> 关联工具：`state_*` 与 `record_*` 系列 LLM 工具
> （`gsuid_core/ai_core/state_store/tools.py` / `record_tools.py`）。

代理人格 / 业务画像通过 `state_*` 与 `record_*` 工具往 `AIPersistentState` 表里
写入"虚拟账户余额""持仓表""交易流水""签到名单""学习进度"等持久化业务数据。
本组 API 给主人 / 前端**只读 + 兜底删除**的能力，让看板之外的代理"暗箱"
状态变得透明可查。

数据结构速览：

| 字段 | 说明 |
|------|------|
| `scope` | 数据隔离范围。代理工具默认按事件来源分组：`user:<user_id>` / `group:<group_id>` / `global` |
| `state_key` | 业务键名。`state_*` 直接命名；`record_*` 工具自动加 `record:` 前缀（如 `record:stock:account`） |
| `value` | JSON 序列化后的 value（字符串/数字/列表/字典） |
| `version` | 乐观锁版本号（`record_*` 写入累加） |
| `expire_at` | 可选 TTL；为空 = 永久 |

**重要**：本 API **不提供写入端点**。写入由代理 / 插件通过工具完成，避免人工
改值导致 UI 与代理逻辑状态分裂。如需修改请让代理自行用 `record_update` /
`state_set` 重写，或经主人格指令调用对应工具。

---

## 1. 列出所有 scope

```
GET /api/ai/state-store/scopes
```

按 key 数倒序返回所有出现过的 scope：

```jsonc
{
  "status": 0,
  "data": {
    "scopes": [
      {"scope": "user:user_web_01", "key_count": 12},
      {"scope": "group:1779024006344", "key_count": 7},
      {"scope": "global", "key_count": 2}
    ],
    "count": 3
  }
}
```

UI 建议把它做成下拉框 / 侧边栏，让主人选 scope 后再列 keys。

---

## 2. 列出某 scope 下的 keys

```
GET /api/ai/state-store/keys?scope=user:user_web_01[&prefix=record:&include_expired=false]
```

| 参数 | 说明 |
|------|------|
| `scope` | 必填，从端点 1 拿到的 scope |
| `prefix` | 可选 state_key 前缀过滤。常见值：`record:`（只看结构化集合）、`self_model`（看自我认知）等 |
| `include_expired` | 默认 false——TTL 已过期的行不返回 |

返回每个 key 的元信息（不展开 value，避免大 JSON 撑爆响应）：

```jsonc
{
  "status": 0,
  "data": {
    "items": [
      {
        "scope": "user:user_web_01",
        "state_key": "record:stock:account",
        "version": 4,
        "size_bytes": 412,
        "created_at": "2026-05-24T21:32:23",
        "updated_at": "2026-05-25T09:30:01",
        "expire_at": null,
        "value_type": "dict",            // dict / list / scalar / null / string
        "is_record_collection": true,    // 以 record: 开头时 true
        "record_collection_name": "stock:account"
      },
      {
        "scope": "user:user_web_01",
        "state_key": "self_notes",
        "version": 9,
        "size_bytes": 80,
        "...": "..."
      }
    ],
    "count": 12
  }
}
```

`is_record_collection=true` 的行**建议加一个"展开看记录"按钮**，跳到端点 4。
其它行直接用端点 3 取 value 完整 JSON 渲染。

---

## 3. 取单条 (scope, state_key) 的完整 value

```
GET /api/ai/state-store/get?scope=...&state_key=...
```

```jsonc
{
  "status": 0,
  "data": {
    "scope": "user:user_web_01",
    "state_key": "self_notes",
    "version": 9,
    "size_bytes": 80,
    "value_type": "list",
    "is_record_collection": false,
    "value": ["我承诺7天后给主人出研报", "主人偏好简短答复"],
    "...": "..."
  }
}
```

`value` 字段已经 `json.loads` 解析；非法 JSON 时退回原字符串（兼容历史脏数据）。

key 不存在 / 已过期：

```jsonc
{"status": 1, "msg": "key 不存在: user:user_web_01/foo", "data": null}
```

---

## 4. `record_*` 集合分页展开

```
GET /api/ai/state-store/records?scope=...&collection=...[&limit=50&offset=0&where_field=...&where_value=...]
```

| 参数 | 说明 |
|------|------|
| `scope` | 必填，scope 字符串 |
| `collection` | 必填，**不含 `record:` 前缀**——如 `stock:account` / `daily_checkin` / `study_plan` |
| `limit` | 默认 50，最大 500 |
| `offset` | 偏移量（分页） |
| `where_field` / `where_value` | 可选字段相等过滤（与 `record_list` LLM 工具同语义） |

内部数据形态：`record:<collection>` 对应的 value 是 `{record_id: payload_dict}` 的
JSON 字典。本端点把字典拍平成 `[{_rid: ..., **payload}, ...]` 给前端，方便直接
用 table 展示——格式跟 `record_list` LLM 工具的返回一致。

```jsonc
{
  "status": 0,
  "data": {
    "records": [
      {
        "_rid": "main_account",
        "balance": 300000,
        "initial_balance": 300000,
        "asset_type": "virtual_stock_portfolio",
        "created_at": "2026-05-24T21:32:23",
        "status": "active"
      }
    ],
    "total": 1,
    "limit": 50,
    "offset": 0,
    "collection": "stock:account",
    "scope": "user:user_web_01"
  }
}
```

集合不存在 / 被非 record_* 写法覆盖（不是 dict 结构）时：

```jsonc
{
  "status": 0,
  "data": {
    "records": [],
    "total": 0,
    "warning": "集合不存在或被非 record_* 写法覆盖（不是 dict 结构）。"
  }
}
```

UI 建议：用 React table 渲染 `records` 数组，列头从首条 record 的 keys 推断；
点击行展开 raw JSON。

---

## 5. 删除单条 (scope, state_key)（兜底清理用）

```
DELETE /api/ai/state-store/entry?scope=...&state_key=...
```

**⚠️ 请谨慎**：代理人格依赖某些 key 的存在做业务推进（如虚拟账户初始化标志）；
删除后可能导致代理重新初始化或报错。建议：

1. 先用端点 3 / 4 看 value，确认这条状态确实是脏数据 / 已废弃；
2. 删除前在 UI 上加一个二次确认弹窗；
3. 删完后回头看 Kanban 板，确认对应任务不会因状态丢失被卡死。

```jsonc
{"status": 0, "data": {"scope": "...", "state_key": "..."}}
```

不存在时返回 `status=1`。

---

## 6. 批量删除多条（前端"勾选多行 → 一键删除"）

```
POST /api/ai/state-store/entries/batch-delete
Content-Type: application/json
```

适用于 UI 表格里勾选多行后一次性清理的场景。底层走单条 SQL `IN` 查询 + 单条
`DELETE`，比循环调端点 5 省掉 N 次往返。

请求体支持**两种填法**，可单用也可混填（最终合并去重）：

**模式 A · 跨 scope 列表**（任意 (scope, state_key) 对）：

```jsonc
{
  "entries": [
    {"scope": "user:user_web_01", "state_key": "self_notes"},
    {"scope": "group:1779024006344", "state_key": "record:checkin"}
  ]
}
```

**模式 B · 同 scope 简写**（更省字节，前端在某 scope 详情页常用）：

```jsonc
{
  "scope": "user:user_web_01",
  "state_keys": ["self_notes", "record:stock:account", "record:study_plan"]
}
```

| 字段 | 说明 |
|------|------|
| `entries` | 可选，`[{scope, state_key}, ...]` 列表 |
| `scope` + `state_keys` | 可选，同 scope 批量简写 |

**单次上限 500 条**；超出返回 `status=2`，避免误传无界列表打空整表。

返回逐条结果，让前端在表格里把"成功删除"和"key 不存在"分别标识：

```jsonc
{
  "status": 0,
  "msg": "ok",
  "data": {
    "requested_count": 3,
    "deleted_count": 2,
    "not_found_count": 1,
    "results": [
      {"scope": "user:user_web_01", "state_key": "self_notes",         "deleted": true,  "reason": null},
      {"scope": "user:user_web_01", "state_key": "record:stock:account","deleted": true,  "reason": null},
      {"scope": "user:user_web_01", "state_key": "record:study_plan",  "deleted": false, "reason": "not_found"}
    ]
  }
}
```

错误返回：

- 目标列表为空（两种填法都没给出有效条目）：`status=1`，`msg="目标列表为空..."`
- 超过单次上限：`status=2`，`msg="单次批量删除上限 500 条..."`

UI 建议：

1. 勾选行 → 点"批量删除"按钮 → **弹窗列出所有待删 key 让主人逐条复核**（批量
   操作影响面更大，二次确认比单删更重要）；
2. 服务端返回后按 `results[].deleted` 把成功的行从表格里移除，把
   `not_found` 的行做高亮提示（通常是前端缓存与后端不一致，刷新一次即可）；
3. 删完后建议刷新一次 Kanban 板，确认对应任务不会因状态丢失被卡死。

---

## 7. 与 Kanban / Artifact API 的关系

- **Kanban API**（`/api/ai/kanban/*`）：看任务树状态、子任务、artifact——回答
  "代理跑到哪一步了？产物是什么？"
- **Artifact API**（`/api/ai/artifacts/*`）：看具体产物——回答"那张图 / 那段
  报告原文长什么样？"
- **State Store API**（本组，`/api/ai/state-store/*`）：看代理维护的持久化业务
  状态——回答"虚拟账户余额是多少？持仓里有几只股票？打卡了几天？"

三组 API 一起覆盖了"代理人格做事 → 产物可追溯 + 业务状态可查"的完整可见性。

---

## 8. 权限

所有端点均挂 `require_auth`（与其它 webconsole API 一致），只有登录用户能调。

如未来要细粒度按 scope 鉴权（如不让 user A 看 user B 的 state），需在 `require_auth`
后追加 owner_user_id 校验——当前实现假定 webconsole 管理员视角可查全部 scope。
