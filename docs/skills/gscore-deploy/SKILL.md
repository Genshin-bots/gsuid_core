---
name: gscore-deploy
description: >
  当用户要求"部署 GsCore / gsuid_core"、"搭建早柚核心"、"把 Core 跑起来"、"Core 启动失败
  / 报错"、"Core 配置 WebConsole / 网页控制台"、"连接 NoneBot2 / AstrBot / Koishi 等
  Bot"、"安装 / 更新 / 卸载插件"、"切换 Git 镜像源"、"用 Docker 部署"、"挂载模式 /
  Bundle 模式"、"MySQL 切换"、"配置 WS_TOKEN / TRUSTED_IPS"、"公网部署"、"配置 AI 核心
  启用 OpenAI 兼容 API / Tavily / 嵌入模型"、"升级 v3 到 v4"、"core 启动报错排查"
  "Docker 内 git 代理配置"、"WebConsole 注册码忘记"、"Core 与 Bot 不在同一台机器上"
  "HTTPS 与 WebConsole 加密握手" 时触发此 SKILL。

  GsCore（gsuid_core）项目面向部署者的完整指南：覆盖环境与依赖检查、四种 Python
  包管理器（uv / poetry / pdm / pip）的安装与启动、Docker 两种部署模式
  （mount / bundle）、配置文件体系（config.json / core_config.json /
  ai_config.json / openai_config.json / tavily_config.json）、WebConsole
  体系（含注册码 / 加密握手 / 限流）、WebSocket 安全（WS_TOKEN /
  TRUSTED_IPS / 失败封禁 / IP 限流）、插件管理体系（命令安装 / 手动安装 /
  Git 镜像源 / 自动更新）、Bot 适配器连接清单（NoneBot2 / Hoshino /
  AstrBot / ZeroBot / YunZai / Koishi / XYBotV2 / napcat / gs-core-adapter）、
  数据库配置（SQLite 默认 / MySQL / PostgreSQL / 自定义 URL）、AI 核心部署
  关键开关与外部服务（OpenAI 兼容 API / 嵌入 / Rerank / Qdrant / Tavily /
  Exa / MCP）、从 GenshinUID v3 迁移、故障排查清单、目录与路径速查。
---

# GsCore 部署者完整指南（核心入口）

> 本 SKILL 已按章节拆分为主入口 + `references/` 子文档的形式组织。Agent 在需要某
> 专题细节时，顺着下文的相对路径按需 `ReadFile` 加载对应文件，**不要**一次性把
> 所有内容塞进上下文。

## 文档目录索引

| 章节 | 主题 | 链接 |
|------|------|------|
| 一 | 环境与依赖（Python 版本 / git / 包管理器 / 平台特性） | [references/01-environment.md](./references/01-environment.md) |
| 二 | 源码部署安装（克隆 / 四种包管理器安装 / 安装位置） | [references/02-install.md](./references/02-install.md) |
| 三 | 启动 Core（四种启动命令 / 命令行参数 / systemd / supervisor / Docker 内启动） | [references/03-startup.md](./references/03-startup.md) |
| 四 | 配置体系总览（config.json / core_config.json / 插件独立配置文件 / AI 配置文件） | [references/04-config-overview.md](./references/04-config-overview.md) |
| 五 | `config.json` 字段详解（HOST / PORT / WS_TOKEN / masters / sv / command_start / misfire_grace_time / log 等） | [references/05-config-json.md](./references/05-config-json.md) |
| 六 | `core_config.json` 字段详解（自动更新 / 自动重启 / 重启命令 / 风控文案 / 转图阈值 / 代理 / 镜像源等） | [references/06-core-config-json.md](./references/06-core-config-json.md) |
| 七 | WebSocket 安全（WS_TOKEN / TRUSTED_IPS / 失败封禁 / 鉴权流程 / 公网部署） | [references/07-security-ws.md](./references/07-security-ws.md) |
| 八 | WebConsole 网页控制台（地址 / 注册码 / 功能矩阵 / ECDH+AES-GCM 加密握手 / IP 限流） | [references/08-webconsole.md](./references/08-webconsole.md) |
| 九 | 插件管理体系（命令安装 / 手动安装 / 卸载 / 更新 / Git 镜像源切换 / 自动更新策略） | [references/09-plugins.md](./references/09-plugins.md) |
| 十 | 链接 Bot 适配器清单（NoneBot2 / Hoshino / AstrBot / ZeroBot / YunZai / Koishi / XYBotV2 / napcat / Java 适配器） | [references/10-bots.md](./references/10-bots.md) |
| 十一 | 数据库配置（SQLite 默认 / MySQL / PostgreSQL / 自定义 URL / 备份与迁移） | [references/11-database.md](./references/11-database.md) |
| 十二 | Docker 部署两种模式（挂载模式 mount / 全量模式 bundle）+ .env 配置 + 代理与镜像源 | [references/12-docker.md](./references/12-docker.md) |
| 十三 | AI 核心部署要点（ai_config / 模型配置 / 嵌入 / Rerank / Qdrant / Tavily / Exa / MCP / 资源消耗） | [references/13-ai.md](./references/13-ai.md) |
| 十四 | 升级与热更新（Core 自身 / 插件 / 数据迁移 / v3→v4 / 配置文件迁移） | [references/14-upgrade.md](./references/14-upgrade.md) |
| 十五 | 数据目录与路径速查（`data/` 结构 / 数据库文件 / 日志 / 主题 / 备份 / WebConsole dist） | [references/15-data-layout.md](./references/15-data-layout.md) |
| 十六 | 故障排查清单（启动失败 / WS 拒连 / WebConsole 拒登 / Docker 代理 / 依赖冲突 / 资源下载 / 风控） | [references/16-troubleshooting.md](./references/16-troubleshooting.md) |
| 十七 | 常用内置命令速查（`core` 命令 / 权限 / PM 等级 / 前缀 / 黑名单） | [references/17-commands.md](./references/17-commands.md) |

## 推荐部署流程（按顺序阅读 / 跳转）

1. **环境检查**：先看 [一、环境与依赖](./references/01-environment.md) 确认 Python（≥3.11 <4.0）、git、包管理器就绪。
2. **选择部署方式**：
   - 源码裸跑 → [二、源码部署安装](./references/02-install.md) + [三、启动 Core](./references/03-startup.md)
   - Docker 容器 → [十二、Docker 部署](./references/12-docker.md)（新手选「挂载模式」）
3. **首次配置**：[四、配置体系总览](./references/04-config-overview.md) 了解全貌，然后按 [五、`config.json`](./references/05-config-json.md) 把 `HOST / PORT / masters / WS_TOKEN` 改成自己需要的；`core_config.json` 强烈建议先用 [八、WebConsole](./references/08-webconsole.md) 调整。
4. **链接 Bot**：按 [十、链接 Bot 适配器清单](./references/10-bots.md) 选适配器安装到上游 Bot 端，并在 Core 这边填 `WS_TOKEN`（[七、WebSocket 安全](./references/07-security-ws.md)）。
5. **登录 WebConsole**：浏览器开 `http://<HOST>:<PORT>/app`，用 `config.json` 里的 `REGISTER_CODE` 注册管理员账号（**只能注册一个**）。
6. **安装插件**：参考 [九、插件管理体系](./references/09-plugins.md)。命令安装最方便：`core安装插件GenshinUID`；若 GitHub 拉不动，配置 `core_config.json` 的 `ProxyURL` 或在 WebConsole 切换 Git 镜像源。
7. **数据库**：默认 SQLite 即可，多实例 / 大数据切 MySQL → [十一、数据库配置](./references/11-database.md)。
8. **要 AI 功能**：看 [十三、AI 核心部署要点](./references/13-ai.md) —— 先把外部服务 Key 准备好。
9. **升级与迁移**：v3 数据迁移、Core / 插件升级、配置文件 schema 迁移见 [十四、升级与热更新](./references/14-upgrade.md)。
10. **遇到问题**：直奔 [十六、故障排查清单](./references/16-troubleshooting.md)；找路径看 [十五、数据目录与路径速查](./references/15-data-layout.md)；找命令看 [十七、常用内置命令速查](./references/17-commands.md)。

## 关键概念速记（先看这一段再决定读哪一章）

- **Core 与 Bot 是分离的两端**：Bot（NoneBot2 / AstrBot / …）以 WS 客户端身份连 Core。Core 自己是 FastAPI + WebSocket + APScheduler 服务，**不能独立使用**。详见 [三、启动 Core](./references/03-startup.md) 与 [十、链接 Bot](./references/10-bots.md)。
- **部署方式二选一**：源码裸跑（`uv run core` / `poetry run core` / `pdm run core` / `python -m gsuid_core.core`）或 Docker（挂载模式 / 全量模式 bundle）。详见 [二、源码部署安装](./references/02-install.md) 与 [十二、Docker 部署](./references/12-docker.md)。
- **四个层级配置文件**：`data/config.json`（Core 全局 + sv 权限矩阵）、`data/core_config.json`（行为开关 / 自动更新 / 风控）、`data/plugins_configs/<plugin>.json`（每个插件独立文件，旧 `config.json["plugins"]` 已自动迁移）、`data/ai_core/*.json`（AI 子系统）。详见 [四、配置体系总览](./references/04-config-overview.md)。
- **安全三件套**：WS 连接用 `WS_TOKEN`（Core 与 Bot 必须一致）；外网部署必须配 `WS_TOKEN` 或 `TRUSTED_IPS`；WebConsole 走 ECDH+AES-256-GCM 应用层加密握手，登录用 `REGISTER_CODE` 注册；另有 IP 维度滑动窗口限流（Web 端 60s/10 次、5 次连败封禁 900s）。详见 [七、WebSocket 安全](./references/07-security-ws.md) 与 [八、WebConsole](./references/08-webconsole.md)。
- **插件安装两条路径**：
  - 命令行（需 master 权限）`core安装插件<名字>` → 走 `https://docs.sayu-bot.com/plugin_list.json` 索引 → 默认从 `cnb.cool` 镜像拉，镜像同步不到时**自动 fallback 到 GitHub**（重要! 这是最近一次 commit 引入的逻辑）。
  - 手动 `git clone` 到 `gsuid_core/plugins/` 后 `core重启`。
  - 详见 [九、插件管理体系](./references/09-plugins.md)。
- **数据库默认 SQLite**：路径 `data/GsData.db`；切换 MySQL 需先 `uv pip install aiomysql` 或 `asyncmy`（按驱动选择），然后 WebConsole 里改数据库类型 / 主机 / 端口 / 用户名 / 密码并重启；PostgreSQL 代码已有但文档标注暂不支持。详见 [十一、数据库配置](./references/11-database.md)。
- **端口 / 监听地址**：`config.json` 的 `HOST` 支持 `localhost`（默认，仅本机可连）/ `0.0.0.0` / `dual` / `none` / `all`，`PORT` 默认 `8765`；命令行可用 `core --host 0.0.0.0 --port 9527` 临时覆盖（不会写回文件）。详见 [三、启动 Core §3.1](./references/03-startup.md#31-命令行参数)。
- **自动更新三开关**：`AutoUpdateCore`（默认开，凌晨 3:40 拉）、`AutoUpdatePlugins`（默认开，4:10 拉）、`AutoRestartCore`（默认关，4:40 重启）。仅 Core / 插件自动更新，**不会自动重启**（生产环境强烈建议把 `AutoRestartCore` 打开并配合 systemd / Docker `--restart always`）。详见 [六、`core_config.json` §6.3](./references/06-core-config-json.md#63-自动更新与重启策略)。
- **AI 核心默认关闭**：`ai_config.json` 的 `enable=false`；启用需先在 WebConsole 填模型 provider（OpenAI 兼容）与外部服务 Key（Tavily / Exa）。详见 [十三、AI 核心部署要点](./references/13-ai.md)。
- **WebConsole 默认自动启动**：无需开关，启动后访问 `http://HOST:PORT/app`，注册码在 `config.json` 的 `REGISTER_CODE` 字段（首次启动随机生成，每个实例都不同）；**只能注册一个管理员账号**。详见 [八、WebConsole §8.2](./references/08-webconsole.md#82-地址--注册码)。
- **v3 → v4 数据迁移**：v3 数据导出成文件夹后拷贝到 `data/<plugin_name>/` 下，删内部 `config.json`，启动后用 master 账号发 `导入v3数据`。详见 [十四、升级与热更新 §14.2](./references/14-upgrade.md#142-v3-到-v4-数据迁移)。
- **数据持久化三件套（Docker）**：`/gsuid_core/data`（玩家账号 / DB / 插件配置 / AI 配置 / 主题 / 日志）、`/gsuid_core/gsuid_core/plugins`（插件目录，方便在宿主机直接管理）、`/venv`（命名卷持久化 Python 虚拟环境，跨镜像升级后手动安装的包会丢）。详见 [十二、Docker §12.2](./references/12-docker.md#122-挂载点与持久化)。
- **内置命令 vs 命令头**：`masters`（pm=0）/ `superusers`（pm=1）/ `command_start`（命令头，默认空；填了之后所有命令都必须带命令头）；`sv` 字段控制每个服务的 pm / black_list / white_list / area。详见 [五、`config.json`](./references/05-config-json.md) 与 [十七、命令速查](./references/17-commands.md)。
- **公网部署必须做的两件事**：① Core 这边把 `HOST` 改成 `0.0.0.0`；② 配 `WS_TOKEN` 或 `TRUSTED_IPS`，否则 Core 启动时会「所有外网 WS 连接将被拒绝」。详见 [七、WebSocket 安全 §7.2](./references/07-security-ws.md#72-公网部署)。
- **资源下载**：首次启动会自动从镜像站 / CDN 拉资源（图片 / wiki / 圣遗物图），慢可挂代理或手动下载资源包覆盖到 `data/<plugin>/resource/`。详见 [十六、故障排查 §16.7](./references/16-troubleshooting.md#167-资源下载过慢或失败)。

## 关联文档（本 SKILL 文件夹内）

- 插件开发者视角的部署：[`../gscore-plugin-development/SKILL.md`](../gscore-plugin-development/SKILL.md)
- 上游 Bot 适配器（NoneBot2 等）：见 [十、链接 Bot](./references/10-bots.md) 章节内引用

## 关联文档（同仓库其他位置）

- AI Agent 总架构：[`docs/AI_AGENT_ARCHITECTURE.md`](../../AI_AGENT_ARCHITECTURE.md)
- AI 触发流程 / 框架开发：[`docs/skills/gscore-development/SKILL.md`](../gscore-development/SKILL.md)
- 启动时序与生命周期：[`gscore-development §二`](../gscore-development/references/02-startup-lifecycle.md)
- 记忆系统：[`docs/MEMORY_SYSTEM.md`](../../MEMORY_SYSTEM.md)
- MCP Server：[`docs/MCP_SERVER.md`](../../MCP_SERVER.md)
- LLM.md（Bot 内部连接管理红线）：仓库根目录 `docs/LLM.md`

## 关联文档（用户文档站点）

- 在线文档站：https://docs.sayu-bot.com
- 快速开始：[`GenshinUID-docs/docs/Started/InstallCore.md`](../../../../../GenshinUID-docs/docs/Started/InstallCore.md)
- Docker 部署：[`GenshinUID-docs/docs/Started/DockerCore.md`](../../../../../GenshinUID-docs/docs/Started/DockerCore.md)
- 配置：[`GenshinUID-docs/docs/Started/CoreConfig.md`](../../../../../GenshinUID-docs/docs/Started/CoreConfig.md)
- 安全：[`GenshinUID-docs/docs/Started/Secure.md`](../../../../../GenshinUID-docs/docs/Started/Secure.md)
- WebConsole：[`GenshinUID-docs/docs/Started/WebConsole.md`](../../../../../GenshinUID-docs/docs/Started/WebConsole.md)
- 插件安装：[`GenshinUID-docs/docs/InstallPlugins/InstallPlugins.md`](../../../../../GenshinUID-docs/docs/InstallPlugins/InstallPlugins.md)
- 适配器清单：[`GenshinUID-docs/docs/LinkBots/AdapterList.md`](../../../../../GenshinUID-docs/docs/LinkBots/AdapterList.md)
- FAQ：[`GenshinUID-docs/docs/Extra/FAQ.md`](../../../../../GenshinUID-docs/docs/Extra/FAQ.md)
