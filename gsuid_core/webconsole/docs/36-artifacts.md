# Artifact Hub API - `/api/ai/artifacts`

> 后端实现：`gsuid_core/webconsole/artifacts_api.py`（2026-05-22 落地，v2 多代理协作）
>
> 数据来源：`gsuid_core/ai_core/planning/models.py` 的 `AIAgentArtifact` 表。
>
> 关联文档：[35-kanban.md](./35-kanban.md) / [37-workspace.md](./37-workspace.md)

「Artifact Hub」是 v2 多代理任务树之间传递结构化产出的总账。每条 artifact 有
唯一 `res_xxx` 句柄、归属一棵任务树（`root_task_id`）下的某个节点（`task_id`），
落盘路径形如 `data/ai_core/artifacts/<root>/<task>/<res_id>/payload.<ext>`。

**类型（`artifact_kind`）**：

| 值 | 来源 | 说明 |
|----|------|------|
| `output` | LLM 工具 `artifact_put` | 子任务的主交付物 |
| `workspace_file` | 命令工具执行后自动扫描 | 命令在 workspace 写入的文件 |
| `log` | （保留） | 中间日志 |
| `report` | （保留） | 报告类 |
| `patch` | webconsole / 上传 / 主动审查 | 待应用的代码 patch |

**inline vs 落盘**：≤4KB 文本走 `payload_inline`（直接返回），更大走
`payload_path` 落盘（用 raw 端点下载）。

---

## 1. 列表

```
GET /api/ai/artifacts?root_task_id=task_xxx
GET /api/ai/artifacts?task_id=task_yyy
```

二选一。

```jsonc
{
  "data": {
    "count": 12,
    "items": [
      {
        "id": "res_a1b2c3d4e5f6",
        "root_task_id": "task_xxx",
        "task_id": "task_yyy",
        "parent_task_id": "task_xxx",
        "from_profile": "internal_reporter",
        "artifact_kind": "output",
        "mime": "text/markdown",
        "summary": "周报草稿 v1",
        "size_bytes": 1234,
        "has_inline": true,
        "has_payload_path": false,
        "payload_path": "",
        "created_at": "2026-05-22T10:11:12",
        "expires_at": "2026-06-21T10:11:12"
      }
    ]
  }
}
```

---

## 2. 详情 + 预览

```
GET /api/ai/artifacts/{res_id}
```

返回元数据 + `payload_preview`（inline 整段；或落盘前 8000 字预览）：

```jsonc
{
  "data": {
    "id": "res_...",
    "summary": "...",
    "has_inline": true,
    "payload_preview": "# 本周热点\n- 话题1...\n"
  }
}
```

---

## 3. 下载原始 payload

```
GET /api/ai/artifacts/{res_id}/raw
```

仅当 `has_payload_path=true` 时返回 `FileResponse`，`Content-Type` 取 `mime` 字段。
inline 类型应直接用详情端点。

---

## 4. 删除

```
DELETE /api/ai/artifacts/{res_id}
```

同时清理落盘文件（若存在）和数据库行。前端应弹确认。

---

## 5. 延长 TTL

```
POST /api/ai/artifacts/{res_id}/extend-ttl?days=30
```

把 `expires_at` 重设为"now + days"。当前框架**没有自动清理任务**——TTL 字段
预留给未来的周期清理（设计稿 §5.2）。

---

## 6. 前端界面建议

### 6.1 任务详情里的 Artifact Tab

- 表格：`res_id` 短句柄（前 8 位）+ `artifact_kind` 图标 + `summary` + 来源画像 +
  大小 + 时间。
- 行点击展开 inline / 预览；落盘文件展示「下载」按钮。
- `from_profile` 用与画像表一致的颜色 Tag。

### 6.2 跨树查询禁止说明

- 后端 API 不限制跨任务树查询 artifact（管理员视角），但 **LLM `artifact_get`
  工具会按 `root_task_id` 校验**——前端只是查看，不会让 AI 越界读到别人任务树的
  产出。

### 6.3 安全提示

- inline 内容可能含敏感数据（如评估结果里出现的用户句柄、生成的报告）；
  前端展示时遵从用户授权层级。
- `extend-ttl` 端点最大 365 天，避免无限延长导致磁盘占用失控。
