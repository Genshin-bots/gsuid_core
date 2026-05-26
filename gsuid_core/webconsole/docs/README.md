# GsCore WebConsole 后端 API 设计文档

## 概述

GsCore WebConsole 提供基于 FastAPI 的 RESTful API，供前端 React 应用调用。所有 API 均以 `/api` 为前缀，采用 JSON 格式交互。

**认证方式**：除特殊说明外，所有 API 需通过 `Authorization: Bearer <token>` Header 携带访问令牌。

**通用响应格式**：
```json
{
    "status": 0,
    "msg": "ok",
    "data": {}
}
```
- `status`: 0=成功，1=失败，其他=错误码
- `msg`: 状态描述
- `data`: 响应数据

---

## 目录

1. [认证 API - /api/auth](./01-auth.md)
2. [系统 API - /api/system](./02-system.md)
3. [插件 API - /api/plugins](./03-plugins.md)（含插件 ICON 图片接口）
4. [核心配置 API - /api/core](./04-core-config.md)
5. [数据库 API - /api/database](./05-database.md)
6. [备份 API - /api/backup](./06-backup.md)
7. [日志 API - /api/logs](./07-logs.md)
8. [调度器 API - /api/scheduler](./08-scheduler.md)
9. [仪表盘 API - /api/dashboard](./09-dashboard.md)
10. [消息推送 API - /api/BatchPush](./10-batch-push.md)
11. [图片资源 API - /api/assets](./11-assets.md)
12. [主题配置 API - /api/theme](./12-theme.md)
13. [Persona API - /api/persona](./13-persona.md)
14. [AI Tools API - /api/ai/tools](./14-ai-tools.md)
15. [AI Skills API - /api/ai/skills](./15-ai-skills.md)
16. [AI Knowledge Base API - /api/ai/knowledge](./16-ai-knowledge.md)
18. [History Manager API - /api/history](./18-history.md)
19. [AI Image RAG API - /api/ai/images](./19-ai-images.md)
20. [AI Statistics API - /api/ai/statistics](./20-ai-statistics.md)
21. [AI Scheduled Task API - /api/ai/scheduled_tasks](./21-ai-scheduled-tasks.md)
22. [AI Memory API - /api/ai/memory](./22-ai-memory.md)
23. [AI Session Logs API - /api/ai/session_logs](./23-ai-session-logs.md)
24. [Provider Config API - /api/provider_config](./24-provider-config.md)
25. [Git 镜像源管理 API - /api/git-mirror](./25-git-mirror.md)
26. [MCP Config API - /api/ai/mcp](./26-mcp-config.md)
27. [嵌入模型配置 API - /api/embedding_config](./27-embedding-config.md)
28. [Git 版本管理 API - /api/git-update](./28-git-update.md)
29. [表情包管理 API - /api/meme](./29-meme.md)
30. [AI 配置向导 API - /api/ai/wizard](./30-ai-wizard.md)
31. [版本信息 API - /api/version](./31-version.md)
32. [Agent Debug API - /api/agent_debug](./32-agent-debug.md)
33. [Capability Agents API - /api/ai/capability-agents](./34-capability-agents.md)
34. [Agent Mesh Kanban API - /api/ai/kanban](./35-kanban.md)
35. [Artifact Hub API - /api/ai/artifacts](./36-artifacts.md)
36. [Artifact Workspace API - /api/ai/kanban/tasks/.../workspace](./37-workspace.md)

---

## 附录

- [错误码说明、用户角色、权限等级](./appendix.md)
