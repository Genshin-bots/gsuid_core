# 十五、数据目录与路径速查

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[十四、升级与热更新](./14-upgrade.md) · **下一章**：[十六、故障排查清单](./16-troubleshooting.md)

部署 GsCore 时**最容易踩的坑**就是找不到某个文件在哪里。本章把所有运行时目录
与文件按用途归档。

> 用户文档对应：https://docs.sayu-bot.com/Advance/DataStruct.html

## 15.1 顶层目录布局

```
gsuid_core/                            ← 仓库根
├── gsuid_core/                        ← Core Python 包
│   ├── plugins/                       ← 业务插件目录（git clone 到这）
│   │   ├── GenshinUID/
│   │   ├── StarRailUID/
│   │   └── ...
│   ├── buildin_plugins/               ← 内置插件（不可卸）
│   ├── webconsole/                    ← WebConsole 后端
│   ├── ai_core/                       ← AI 子系统
│   └── ...
├── data/                              ← 运行时数据（启动时生成）
│   ├── config.json                    ← Core 全局配置
│   ├── core_config.json               ← Core 行为配置
│   ├── GsData.db                      ← SQLite 数据库
│   ├── GsCore_BACKUP_PATH/            ← 自动备份
│   ├── theme_config.json              ← WebConsole 主题
│   ├── dist/                          ← WebConsole 前端 bundle
│   ├── logs/                          ← 日志（保留 8 天）
│   ├── plugins_configs/               ← 每插件独立配置
│   ├── ai_core/                       ← AI 子系统配置 + 数据
│   ├── avatars/                       ← WebConsole 用户头像
│   ├── IMAGE_TEMP/                    ← 临时图片缓存
│   ├── DATA_CACHE_PATH/               ← 通用数据缓存
│   ├── GsCore/                        ← 业务插件数据示例
│   │   ├── database_backup/
│   │   └── ...
│   └── <plugin_name>/                 ← 各业务插件自己的数据
│       ├── config.json                ←    插件自有配置
│       ├── resource/                  ←    资源（图片 / 字体 / 攻略）
│       ├── players/                   ←    玩家数据
│       ├── bg/                        ←    自定义背景图
│       ├── chbg/<角色名>/             ←    自定义角色图
│       └── database_backup/
├── pyproject.toml                     ← 依赖描述
├── uv.lock / poetry.lock / pdm.lock   ← 锁文件
├── .venv/                             ← 虚拟环境
├── docs/                              ← 文档
├── docker-compose.yml / .bundle.yml   ← Docker 配置
├── Dockerfile                         ← Docker 构建
└── .env / .env.example                ← Docker 环境变量
```

## 15.2 关键文件清单

| 路径 | 说明 | 备份重要性 |
|------|------|------------|
| `data/config.json` | Core 全局 | ⭐⭐⭐ |
| `data/core_config.json` | Core 行为 | ⭐⭐ |
| `data/plugins_configs/*.json` | 插件配置 | ⭐⭐⭐ |
| `data/ai_core/ai_config.json` | AI 总开关 | ⭐⭐ |
| `data/ai_core/provider_configs/*.json` | 模型 profile（含 Key） | ⭐⭐⭐ |
| `data/ai_core/tavily_config.json` | Tavily Key | ⭐⭐⭐ |
| `data/ai_core/exa_config.json` | Exa Key | ⭐⭐⭐ |
| `data/ai_core/qdrant_config.json` | Qdrant 配置 | ⭐⭐ |
| `data/ai_core/persona/*.json` | 人格 | ⭐⭐ |
| `data/ai_core/knowledge_base/` | RAG 知识库（按 plugin / agent） | ⭐⭐ |
| `data/ai_core/memory/` | 用户记忆 | ⭐⭐ |
| `data/ai_core/mcp_config/` | MCP server 配置 | ⭐⭐ |
| `data/GsData.db` | SQLite 数据库 | ⭐⭐⭐ |
| `data/GsCore/database_backup/GsData_BAK_*.db` | DB 自动备份 | ⭐⭐⭐ |
| `data/<plugin_name>/resource/` | 业务资源（图片等） | ⭐ |
| `data/<plugin_name>/players/` | 玩家数据 | ⭐⭐⭐ |
| `data/<plugin_name>/config.json` | 插件自有配置 | ⭐⭐ |
| `data/theme_config.json` | WebConsole 主题 | ⭐ |
| `data/avatars/` | 用户头像 | ⭐ |
| `data/logs/` | 日志 | ⭐（可重建） |

> ⚠️ **不要把 `data/` 整个丢进 git**（已经在 `.gitignore`）。

## 15.3 关键路径常量

源码：[`gsuid_core/data_store.py`](../../../gsuid_core/data_store.py)

```python
from gsuid_core.data_store import (
    get_res_path,   # 通用：get_res_path() / get_res_path("sub") / get_res_path(["a", "b"])
    gs_data_path,   # data/ 根目录
    RES,            # get_res_path() 别名
    PLUGINS_CONFIGS_PATH,   # data/plugins_configs/
    CONFIGS_PATH,           # data/configs/
    THEME_CONFIG_PATH,      # data/theme_config.json
    LOGS_CONFIG_PATH,       # data/logs/configs/logs_config.json
    AI_CORE_PATH,           # data/ai_core/
    image_res,              # data/IMAGE_TEMP/
    data_cache_path,        # data/DATA_CACHE_PATH/
    backup_path,            # data/GsCore_BACKUP_PATH/
    error_mark_path,        # data/logs/error_reports/
    WEBCONSOLE_PATH,        # gsuid_core/webconsole/
    DIST_PATH,              # gsuid_core/webconsole/dist/
    DIST_EX_PATH,           # data/dist/  (运行时)
)
```

## 15.4 插件目录权限

插件 git clone 时使用**当前用户**，但 Core 进程可能以另一个用户 / root 跑
（Docker / systemd）。**确保插件目录对 Core 进程可写**：

```sh
# Docker：通常 root，没问题
# 源码裸跑：自己 = 自己，没问题
# 跨用户：sudo chown -R <core_user>:<core_group> gsuid_core/plugins/
```

## 15.5 WebConsole 路由模块清单

源码：[`gsuid_core/webconsole/`](../../../gsuid_core/webconsole/)

| 模块 | 路由前缀 | 功能 |
|------|----------|------|
| `app_app.py` | — | 导出 FastAPI `app` 对象 |
| `mount_app.py` | `/app` | 静态前端 |
| `auth_api.py` | `/api/auth/*` | 注册 / 登录 / 改密 / 头像上传 |
| `auth_crypto.py` | — | ECDH + AES-256-GCM 加密握手 |
| `web_api.py` | `/api/web/*` | Web 全局 token / 在线用户 |
| `core_config_api.py` | `/api/core_config/*` | Core 配置读写 |
| `database_api.py` | `/api/database/*` | 表 / 列 / 行管理 |
| `plugins_api.py` | `/api/plugins/*` | 插件列表 / 启停 / 顺序 |
| `plugin_icon_api.py` | `/api/plugin_icon/*` | 插件图标 |
| `backup_api.py` | `/api/backup/*` | 备份策略 / 立即备份 |
| `scheduler_api.py` | `/api/scheduler/*` | 定时任务监控 / 暂停 |
| `logs_api.py` | `/api/logs/*` | 历史 / 实时日志 |
| `system_api.py` | `/api/system/*` | 系统信息（CPU / 内存 / 磁盘） |
| `theme_api.py` | `/api/theme/*` | 主题读写 |
| `dashboard_api.py` | `/api/dashboard/*` | 仪表盘统计 |
| `history_api.py` | `/api/history/*` | 调用历史 |
| `message_api.py` | `/api/message/*` | 消息查询 |
| `workspace_api.py` | `/api/workspace/*` | 工作区（文件浏览） |
| `state_store_api.py` | `/api/state_store/*` | 状态存储 |
| `kanban_api.py` | `/api/kanban/*` | 看板 |
| `git_update_api.py` | `/api/git_update/*` | git 拉取 / 切换源 |
| `git_mirror_api.py` | `/api/git_mirror/*` | Git 镜像源管理 |
| `version_api.py` | `/api/version/*` | 版本信息 |
| `embedding_config_api.py` | `/api/embedding/*` | 嵌入配置 |
| `provider_config_api.py` | `/api/provider_config/*` | 模型 profile |
| `persona_api.py` | `/api/persona/*` | 人格 |
| `knowledge_base_api.py` | `/api/knowledge_base/*` | RAG 知识库 |
| `mcp_config_api.py` | `/api/mcp_config/*` | MCP 配置 |
| `meme_api.py` | `/api/meme/*` | 表情包 |
| `ai_tools_api.py` | `/api/ai_tools/*` | AI 工具注册表 |
| `ai_wizard_api.py` | `/api/ai_wizard/*` | AI 向导 |
| `ai_statistics_api.py` | `/api/ai_statistics/*` | AI 统计 |
| `ai_skills_api.py` | `/api/ai_skills/*` | AI 技能 |
| `ai_session_logs_api.py` | `/api/ai_session_logs/*` | AI 会话日志 |
| `ai_scheduled_task_api.py` | `/api/ai_scheduled_task/*` | AI 定时任务 |
| `ai_performance_api.py` | `/api/ai_performance/*` | AI 性能 |
| `ai_memory_api.py` | `/api/ai_memory/*` | AI 记忆 |
| `agent_debug_api.py` | `/api/agent_debug/*` | Agent 调试 |
| `capability_agents_api.py` | `/api/capability_agents/*` | 能力代理画像 |
| `chat_with_history_api.py` | `/api/chat_with_history/*` | 带历史的对话 |
| `image_rag_api.py` | `/api/image_rag/*` | 图片 RAG |
| `trace_api.py` | `/api/trace/*` | 链路追踪 |
| `artifacts_api.py` | `/api/artifacts/*` | 制品（生成的图片 / 文件） |
| `assets_api.py` | `/api/assets/*` | 资源 |
| `setup_frontend.py` | — | 后台从 CDN 拉前端 bundle |

## 15.6 .gitignore 推荐

`.gitignore`（仓库已有，自部署可加）：

```gitignore
data/
.venv/
.env
__pycache__/
*.pyc
.DS_Store
```

> 但 `data/plugins/` 里的每个插件子目录**自己**是 git 仓库（`plugins/GenshinUID/.git/`），
> 互不影响。

## 15.7 常用路径速查（一行命令）

```sh
# Linux / macOS
ls -la data/

# 看 Core 配置
cat data/config.json | jq .

# 看日志
tail -f data/logs/$(ls -t data/logs/ | head -1)

# 数据库
sqlite3 data/GsData.db ".tables"

# Windows
dir data
type data\config.json
dir data\logs
```

> 完整目录结构：[用户文档 DataStruct.md](../../../../../GenshinUID-docs/docs/Advance/DataStruct.md)
