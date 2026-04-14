# GsCore WebConsole 后端 API 设计文档

> **文档已拆分**，详细内容请查看 [`docs/`](./docs/) 文件夹。

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

---

## 目录

| # | API 类别 | 文档路径 |
|---|---------|---------|
| 1 | 认证 API - /api/auth | [docs/01-auth.md](./docs/01-auth.md) |
| 2 | 系统 API - /api/system | [docs/02-system.md](./docs/02-system.md) |
| 3 | 插件 API - /api/plugins | [docs/03-plugins.md](./docs/03-plugins.md) |
| 4 | 核心配置 API - /api/core | [docs/04-core-config.md](./docs/04-core-config.md) |
| 5 | 数据库 API - /api/database | [docs/05-database.md](./docs/05-database.md) |
| 6 | 备份 API - /api/backup | [docs/06-backup.md](./docs/06-backup.md) |
| 7 | 日志 API - /api/logs | [docs/07-logs.md](./docs/07-logs.md) |
| 8 | 调度器 API - /api/scheduler | [docs/08-scheduler.md](./docs/08-scheduler.md) |
| 9 | 仪表盘 API - /api/dashboard | [docs/09-dashboard.md](./docs/09-dashboard.md) |
| 10 | 消息推送 API - /api/BatchPush | [docs/10-batch-push.md](./docs/10-batch-push.md) |
| 11 | 图片资源 API - /api/assets | [docs/11-assets.md](./docs/11-assets.md) |
| 12 | 主题配置 API - /api/theme | [docs/12-theme.md](./docs/12-theme.md) |
| 13 | Persona API - /api/persona | [docs/13-persona.md](./docs/13-persona.md) |
| 14 | AI Tools API - /api/ai/tools | [docs/14-ai-tools.md](./docs/14-ai-tools.md) |
| 15 | AI Skills API - /api/ai/skills | [docs/15-ai-skills.md](./docs/15-ai-skills.md) |
| 16 | AI Knowledge Base API - /api/ai/knowledge | [docs/16-ai-knowledge.md](./docs/16-ai-knowledge.md) |
| 17 | AI System Prompt API - /api/ai/system_prompt | [docs/17-ai-system-prompt.md](./docs/17-ai-system-prompt.md) |
| 18 | History Manager API - /api/history | [docs/18-history.md](./docs/18-history.md) |
| 19 | AI Image RAG API - /api/ai/images | [docs/19-ai-images.md](./docs/19-ai-images.md) |
| 20 | AI Statistics API - /api/ai/statistics | [docs/20-ai-statistics.md](./docs/20-ai-statistics.md) |
| 21 | AI Scheduled Task API - /api/ai/scheduled_tasks | [docs/21-ai-scheduled-tasks.md](./docs/21-ai-scheduled-tasks.md) |

---

## 附录

- [错误码说明、用户角色、权限等级](./docs/appendix.md)

---

## 快速导航

- [完整目录索引](./docs/README.md)
