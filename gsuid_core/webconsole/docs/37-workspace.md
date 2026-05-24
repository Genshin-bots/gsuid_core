# Artifact Workspace API - `/api/ai/kanban/tasks/{task_id}/workspace`

> 后端实现：`gsuid_core/webconsole/workspace_api.py`（2026-05-22 落地，v2 多代理协作）
>
> 工作区路径：`data/ai_core/artifacts/<root_task_id>/<task_id>/workspace/`
>（设计稿 `docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md` §5.2）。

每个 Kanban 子任务节点都有一个**唯一可写目录**：`workspace`。能力代理的所有
文件 / 命令操作都被框架强制限制在这里——越界写入会被路径守卫拒绝并写
`workspace_violation` 事件日志（参见 [35-kanban.md](./35-kanban.md) §2 详情端点
的 logs 字段）。

本组 API 给前端提供"打开工作区文件夹"的能力，允许：

- 列文件 / 下载单文件；
- 上传文件到 workspace（自动登记为 `workspace_file` artifact）；
- 提交一段 patch 文本作为 `patch` artifact **待人审**——本期不自动 `git apply`。

---

## 1. 列出工作区文件

```
GET /api/ai/kanban/tasks/{task_id}/workspace/files
```

返回 workspace 子树的全部文件清单：

```jsonc
{
  "data": {
    "workspace": "F:/gsuid_core/data/ai_core/artifacts/task_xxx/task_yyy/workspace",
    "files": [
      {
        "path": "report.md",
        "size_bytes": 1234,
        "modified_at": "2026-05-22T10:11:12"
      },
      {
        "path": "code_agent/poster.html",
        "size_bytes": 8765,
        "modified_at": "2026-05-22T10:13:01"
      }
    ]
  }
}
```

`path` 是 workspace 内的相对路径。`code_agent/` 这种子目录由调度器为不同
画像创建（`ensure_workspace(root, task, profile)`）。

---

## 2. 下载单文件

```
GET /api/ai/kanban/tasks/{task_id}/workspace/files/raw?path=report.md
```

防越界：服务端会把 `path` 解析到 workspace 子树内，超出范围 → `status=1`。

返回 `FileResponse(filename=...)`，浏览器一般直接下载。

---

## 3. 上传文件到 workspace

```
POST /api/ai/kanban/tasks/{task_id}/workspace/import
```

multipart/form-data：

- `upload`：要上传的文件；
- `sub_path`（query，可选）：相对 workspace 的子目录，比如 `inputs`。

上传后会**自动登记**为 `workspace_file` artifact（30 天 TTL），返回：

```jsonc
{
  "data": {
    "task_id": "task_yyy",
    "path": "inputs/dataset.csv",
    "size_bytes": 4567,
    "artifact_ids": ["res_xxxxxxxxxxxx"]
  }
}
```

主用场景：**主人给某个子任务"喂"一份输入文件**，让能力代理在下一次唤醒时
可以读取它（通过 `read_file_content` / `execute_file`）。

---

## 4. 提交 patch（待人审）

```
POST /api/ai/kanban/tasks/{task_id}/workspace/apply-patch
```

```json
{
  "patch_text": "diff --git a/src/foo.py b/src/foo.py\n...",
  "summary": "修复 foo 越界",
  "mime": "text/x-patch"
}
```

**重要**：本端点**不**会自动调用 `git apply`。"代理自动写仓库" 属于高风险动作
（设计稿 §5.4.4），框架坚持先把 patch 登记为 artifact 待人工审查。

返回：

```jsonc
{
  "data": {
    "artifact_id": "res_xxxxxxxxxxxx",
    "warning": "patch 已登记为 artifact，但框架不会自动 git apply；请人工审查后再应用。"
  }
}
```

前端 UI 应：

1. 渲染 patch artifact 内容（推荐用 diff 高亮组件）；
2. 旁边放一个「Approve & Apply」按钮——后续可由插件 / 管理员手动在仓库执行
   `git apply`；
3. 不要悄悄替主人 `git apply`，这会绕过审计。

---

## 5. 前端界面建议

### 5.1 工作区面板

- 任务详情抽屉新增 Tab「Workspace」；
- 顶部显示 `workspace` 绝对路径 + 「在文件管理器打开」（仅本地部署时有用）；
- 文件表格：路径 / 大小 / 修改时间 / 操作（下载 / 删除 — 删除走
  `DELETE /api/ai/artifacts/{res_id}`，因为每个文件都对应一个 artifact）；
- 顶部「上传文件」按钮 → `POST /workspace/import`；
- 底部「提交 Patch」按钮（仅 code_agent 任务）→ `POST /workspace/apply-patch`。

### 5.2 越界事件告警

- 任务详情的「事件日志」Tab 里把 `workspace_violation` 事件高亮——这通常意味着
  能力代理尝试写仓库 / 系统目录，应被关注；
- 同一子任务连续 ≥3 次越界（设计稿 §5.4.6）可在前端给"升级为 fail"按钮——
  本期后端尚未自动升级（参见实施记录 §13.3），需要人工调
  `POST /kanban/tasks/{id}/fail`。

### 5.3 安全提示

- 上传文件大小没有硬限制——前端建议在客户端先校验（如 ≤20MB），避免一次性把
  大文件塞到磁盘；
- patch 上传**不会**绕过任何代码审查，前端必须明示主人"提交后还需人工应用"，
  不要做成"一键修复"按钮。
