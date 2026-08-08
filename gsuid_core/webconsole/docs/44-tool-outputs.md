# FileOS 工具落盘 API - `/api/ai/tool-outputs`

> 后端：`gsuid_core/webconsole/tool_outputs_api.py`
> 数据：`AIToolOutputRecord`（SQL）+ 可选磁盘 `_tool_outputs/*.md` + Qdrant 集合 `tool_outputs`
> 与 [36-artifacts.md](./36-artifacts.md)（Kanban `res_*`）是**两套账本**。

达标 ToolReturn（如较长的 `web_search_tool` 结果）经 FileOS 落盘后，本 API 供控制台浏览 / 筛选 / 搜索 / 删除。

**落盘门槛（实现侧）**：正文约 ≥800 字符才会写入；TTL 默认 30 天；同 owner+scope+hash+tool 去重。

---

## 1. 列表

```
GET /api/ai/tool-outputs
```

| 参数 | 说明 |
|------|------|
| `tool_name` | 精确工具名 |
| `owner_user_id` | 所有者 |
| `scope_key` | 群/会话 scope |
| `session_id` | 会话 id |
| `keyword` | 模糊匹配 summary / id / tool_name / session_id |
| `include_expired` | 是否含过期，默认 false |
| `limit` | 1–500，默认 50 |
| `offset` | 分页偏移 |

```jsonc
{
  "status": 0,
  "msg": "ok",
  "data": {
    "items": [
      {
        "id": "to_ab12cd34ef56",
        "tool_name": "web_search_tool",
        "summary": "…",
        "owner_user_id": "…",
        "scope_key": "…",
        "session_id": "…",
        "size_bytes": 12345,
        "has_inline": false,
        "has_payload_path": true,
        "created_at": "…",
        "expires_at": "…"
      }
    ],
    "count": 20,
    "total": 120,
    "limit": 50,
    "offset": 0,
    "has_more": true
  }
}
```

---

## 2. 工具名筛选项

```
GET /api/ai/tool-outputs/meta/tool-names
```

```jsonc
{ "data": { "tool_names": ["web_search_tool", "web_fetch_tool", "…"] } }
```

---

## 3. 详情 + 预览

```
GET /api/ai/tool-outputs/{id}?preview_chars=12000
```

返回元数据 + `payload_preview` / `payload_truncated` / `payload_full_chars`。

---

## 4. 全文下载

```
GET /api/ai/tool-outputs/{id}/raw
```

- 有 `payload_path` → `FileResponse`
- 仅 `payload_inline` → `PlainTextResponse` 附件

---

## 5. 单条删除

```
DELETE /api/ai/tool-outputs/{id}
```

删除 SQL 行、磁盘文件，并清理 Qdrant 索引点。

---

## 6. 批量删除

```
POST /api/ai/tool-outputs/batch-delete
Content-Type: application/json

{ "ids": ["to_xxx", "to_yyy"] }
```

单次最多 500 条。

```jsonc
{ "data": { "deleted": 2, "ids": ["to_xxx", "to_yyy"] } }
```
