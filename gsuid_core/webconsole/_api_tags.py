"""webconsole/_api_tags.py — WebConsole 后台 OpenAPI 中文分层标签集中定义。

WebConsole(amis/React 管理后台)的路由与三个业务插件共享同一个 FastAPI app,
导出的 openapi.json 里靠 ``tags`` 归组。标签用斜杠 ``/`` 分层——**顶层系统 / 大功能
/ 小功能**,导入 Swagger / Apifox / Reqable 会折叠成多级目录。WebConsole 的接口
统一挂在顶层「控制台」之下。

各路由模块 ``from ._api_tags import XXX as _TAG`` 后在装饰器传 ``tags=_TAG``。
类型标注为 ``list[Union[str, Enum]]`` 以匹配 FastAPI ``tags`` 形参(不变类型)。
"""

from __future__ import annotations

from enum import Enum
from typing import Union

_Tag = list[Union[str, Enum]]

_SYS = "控制台"

# ─────────────────────────── 账户 / 系统 ───────────────────────────
AUTH: _Tag = [f"{_SYS}/认证"]
SYSTEM: _Tag = [f"{_SYS}/系统/系统信息"]
VERSION: _Tag = [f"{_SYS}/系统/版本"]
DASHBOARD: _Tag = [f"{_SYS}/系统/仪表盘"]
CORE_CONFIG: _Tag = [f"{_SYS}/系统/核心配置"]
FRAMEWORK_CONFIG: _Tag = [f"{_SYS}/系统/框架配置"]
DATABASE: _Tag = [f"{_SYS}/系统/数据库"]
BACKUP: _Tag = [f"{_SYS}/系统/备份"]
LOGS: _Tag = [f"{_SYS}/系统/日志"]
SCHEDULER: _Tag = [f"{_SYS}/系统/调度器"]
TRACE: _Tag = [f"{_SYS}/系统/链路追踪"]
STATE_STORE: _Tag = [f"{_SYS}/系统/状态存储"]

# ─────────────────────────── 插件 ───────────────────────────
PLUGINS: _Tag = [f"{_SYS}/插件/插件管理"]
PLUGIN_ICON: _Tag = [f"{_SYS}/插件/插件图标"]
GIT_MIRROR: _Tag = [f"{_SYS}/插件/Git 镜像源"]
GIT_UPDATE: _Tag = [f"{_SYS}/插件/Git 更新"]

# ─────────────────────────── 消息 / 资源 ───────────────────────────
MESSAGE: _Tag = [f"{_SYS}/消息推送"]
ASSETS: _Tag = [f"{_SYS}/资源/图片资源"]
THEME: _Tag = [f"{_SYS}/资源/主题"]
BRAND: _Tag = [f"{_SYS}/资源/品牌"]
MEME: _Tag = [f"{_SYS}/资源/表情包"]

# ─────────────────────────── AI ───────────────────────────
AI_TOOLS: _Tag = [f"{_SYS}/AI/工具"]
AI_SKILLS: _Tag = [f"{_SYS}/AI/技能"]
KNOWLEDGE: _Tag = [f"{_SYS}/AI/知识库"]
IMAGE_RAG: _Tag = [f"{_SYS}/AI/图片 RAG"]
AI_STATS: _Tag = [f"{_SYS}/AI/统计"]
AI_SCHED: _Tag = [f"{_SYS}/AI/定时任务"]
AI_MEMORY: _Tag = [f"{_SYS}/AI/记忆"]
AI_SESSION_LOGS: _Tag = [f"{_SYS}/AI/会话日志"]
PROVIDER_CONFIG: _Tag = [f"{_SYS}/AI/供应商配置"]
MCP_CONFIG: _Tag = [f"{_SYS}/AI/MCP 配置"]
EMBEDDING_CONFIG: _Tag = [f"{_SYS}/AI/嵌入模型配置"]
AI_WIZARD: _Tag = [f"{_SYS}/AI/配置向导"]
AGENT_DEBUG: _Tag = [f"{_SYS}/AI/Agent 调试"]
CAPABILITY_AGENTS: _Tag = [f"{_SYS}/AI/能力代理"]
KANBAN: _Tag = [f"{_SYS}/AI/看板"]
ARTIFACTS: _Tag = [f"{_SYS}/AI/Artifact"]
WORKSPACE: _Tag = [f"{_SYS}/AI/工作区"]
AI_PERF: _Tag = [f"{_SYS}/AI/性能"]
BUDGET: _Tag = [f"{_SYS}/AI/预算"]
PERSONA: _Tag = [f"{_SYS}/AI/Persona"]
HISTORY: _Tag = [f"{_SYS}/AI/历史记录"]
CHAT: _Tag = [f"{_SYS}/AI/对话"]
APPROVALS: _Tag = [f"{_SYS}/AI/审批"]
