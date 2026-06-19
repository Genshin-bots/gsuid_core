# 四、配置体系总览

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[三、启动 Core](./03-startup.md) · **下一章**：[五、`config.json` 字段详解](./05-config-json.md)

GsCore 的运行时配置**分散在多个文件 + 多个层级**，新人最容易绕晕。本章先给一张
全景图，后续章节按文件拆开讲。

## 4.1 配置文件层级

```
data/                                ← 运行时数据目录（启动时自动生成）
├── config.json                      ← ① Core 全局配置：HOST / PORT / WS_TOKEN / masters / sv
├── core_config.json                 ← ② Core 行为配置：自动更新 / 重启 / 风控 / 转图 / 代理
├── theme_config.json                ← WebConsole 主题（用户从 Web 改）
├── logs/                            ← 日志目录（保留 8 天）
│   └── configs/logs_config.json     ← 日志配置（备份策略 / 输出）
├── plugins_configs/                 ← ③ 每个插件一个独立 JSON（替代旧 config.json["plugins"]）
│   ├── GenshinUID.json
│   ├── StarRailUID.json
│   └── ...
├── ai_core/                         ← ④ AI 子系统配置目录
│   ├── ai_config.json               ←    AI 总开关 / 嵌入 / Rerank / Qdrant / Tavily / 模式 / 白黑名单
│   ├── openai_config.json           ←    OpenAI 兼容模型 profile 列表
│   ├── anthropic_config.json
│   ├── gemini_config.json
│   ├── provider_configs/            ←    各 provider 的 profile（每个 profile 一个文件）
│   ├── embedding_config/             ←    嵌入 provider 配置
│   ├── rerank_config/
│   ├── tavily_config.json
│   ├── exa_config.json
│   ├── qdrant_config.json
│   ├── persona/                      ←    人格目录
│   ├── knowledge_base/              ←    RAG 知识库（按 plugin / agent 分子目录）
│   ├── memory/                      ←    用户记忆（短期 / 长期 / 画像）
│   ├── mcp_config/                  ←    MCP server 配置
│   └── logs/                        ←    AI 会话日志
├── GsData.db                        ← ⑤ SQLite 数据库（默认）
├── GsCore_BACKUP_PATH/              ← 自动备份目录
└── <plugin_name>/                   ← ⑥ 插件自身数据目录（GenshinUID / StarRailUID / ...）
    ├── config.json                  ←    插件自有配置（与 plugins_configs/*.json 区分！）
    ├── resource/                    ←    资源（图片 / 字体 / 攻略）
    ├── players/                     ←    玩家数据（面板 / 抽卡）
    ├── bg/                          ←    自定义背景图
    ├── chbg/                        ←    自定义角色图
    └── database_backup/
```

> ⑥ 是**插件自己**的目录，命名与 `plugins/<plugin_name>/` 一致。插件自己读写，
> Core 不解释内容。
>
> ③ 是**插件暴露给 WebConsole 的配置**（基于 `CONFIG_DEFAULT` + `StringConfig`），
> 可在 WebConsole「插件参数配置」页签里改。

## 4.2 谁负责写哪个文件

| 文件 | 谁写 | 怎么写 |
|------|------|--------|
| `config.json` | 用户 / WebConsole | 手动编辑，或 WebConsole「核心配置」页签 |
| `core_config.json` | 用户 / WebConsole | 强烈建议 WebConsole「核心配置」页签 |
| `plugins_configs/<plugin>.json` | 插件代码 / 用户 / WebConsole | WebConsole「插件参数配置」页签 |
| `ai_core/*.json` | 用户 / WebConsole | WebConsole「AI 配置」页签 |
| `<plugin>/config.json` | 插件自身代码 | 用户用 Bot 命令（如 `gs设置xxx`）改 |
| `theme_config.json` | WebConsole | WebConsole「主题」页签 |

## 4.3 修改配置的两种方式

### 4.3.1 WebConsole（推荐）

`http://HOST:PORT/app` → 左侧菜单：

- **核心配置**：`config.json` + `core_config.json`（不可热更，必须 `core重启`）
- **插件参数配置**：`plugins_configs/<plugin>.json`（多数可热更，少数需重启）
- **插件功能配置**：插件自己的功能开关（多数可热更）
- **AI 配置**：`ai_core/*.json`

### 4.3.2 直接改 JSON

```sh
# Linux
nano data/config.json

# Windows
notepad data\config.json
```

修改后视字段决定要不要重启：

| 类型 | 是否需要重启 |
|------|--------------|
| `HOST` / `PORT` / `WS_TOKEN` / `masters` / `TRUSTED_IPS` | ✅ 必须重启 |
| `command_start` | ✅ 必须重启 |
| `sv.<name>.enabled` / `pm` / `black_list` 等 | 🔁 大多可热更 |
| `log.level` | ✅ 必须重启 |
| `core_config.json` 全字段 | 🔁 多数可热更 |
| 插件独立配置字段 | 🔁 看插件实现（多数可热更） |

> **强约束**：`config.json` 修改时**别动引号 / 别忘逗号**，坏掉会导致启动
> 直接抛 `JSONDecodeError`。Core 启动时会自动给缺失字段补默认值（见
> [`gsuid_core/config.py:109-125`](../../../gsuid_core/config.py)），所以可以安全
> 删除键让 Core 重生。
>
> ⚠️ Core **不会**自动备份 `config.json`；手动改前先 `cp config.json config.json.bak`。

## 4.4 首次启动会自动发生什么

1. **不存在 `config.json`** → 用 `CONFIG_DEFAULT` 生成（含随机 `REGISTER_CODE`）
2. **不存在 `core_config.json`** → 用 `CORE_CONIFG_DEFAULT` 生成
3. **不存在 `plugins_configs/<plugin>.json`** → 插件首次导入时按 `CONFIG_DEFAULT` 生成
4. **`config.json` 存在 `plugins` 键**（旧版）→ 自动迁移到 `plugins_configs/<plugin>.json`，备份为 `config_backup.json` 后移除 `plugins` 键
5. **不存在 SQLite DB** → 自动建表（依据所有插件的 `BaseModel` 子类）
6. **WebConsole dist** 不存在 → 后台异步从 CDN 下载（`setup_frontend_b`）

## 4.5 字段命名风格约定

- Core 全局配置：SCREAMING_SNAKE（`HOST`, `WS_TOKEN`）
- `sv` 子字段：snake_case（`black_list`, `white_list`）
- 插件配置：snake_case（`auto_signin`, `scheduled_hour`）
- AI 配置：snake_case（`embedding_provider`, `websearch_provider`）

## 4.6 配置文件加载流程

```
core.py main()
  └─ core_config = CoreConfig()           # 读 data/config.json
        └─ 缺字段补默认 + 写回
        └─ PluginConfigStore()             # 读 data/plugins_configs/*.json
              └─ 检测到旧 config.json["plugins"] → 一次性迁移
  └─ init_database()                       # 读 plugins_config.gs_config.database_config
  └─ load_gss()                            # 加载插件，触发各插件的 CONFIG_DEFAULT 初始化
  └─ AI 启动（若 enable=true）
        └─ 读 ai_config / openai_config / tavily_config / ...
```

完整顺序：[`gscore-development §二、启动时序与生命周期`](../../gscore-development/references/02-startup-lifecycle.md)。

## 4.7 下一步

按需跳转：

- [五、`config.json`](./05-config-json.md) — 端口 / Token / 权限
- [六、`core_config.json`](./06-core-config-json.md) — 自动更新 / 重启 / 风控
- [八、WebConsole](./08-webconsole.md) — 怎么用 Web 改
- [十一、数据库配置](./11-database.md) — 切 MySQL
- [十三、AI 核心部署要点](./13-ai.md) — 配外部 Key
