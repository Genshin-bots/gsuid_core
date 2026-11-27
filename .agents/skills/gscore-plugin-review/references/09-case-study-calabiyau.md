# 九、完整实战案例：`CalabiyauUID` 商店准入审核复盘

> 返回 [SKILL 主入口](../SKILL.md)

本案例基于对真实社区插件 `https://github.com/sssysy/CalabiyauUID`（基准 commit `fbc30b0`，161 文件 / 2.78 MB / 98 个 `.py`）的完整审核过程整理，展示如何按本 SKILL 串联扫描、精读、核验与分级定论。

## 9.1 阶段一：静态体检结果

1. **目录与体积**：标准嵌套结构（`__nest__.py` + `__full__.py` + `ICON.png` + `LICENSE(GPL-3.0)` + `pyproject.toml` + `ruff.toml`），体积 2.78 MB，无敏感文件入库。
2. **依赖与导入**：
   - 第三方 import：`PIL`、`aiofiles`、`httpx`、`jinja2`、`pydantic`、`pypinyin`、`sqlalchemy`、`sqlmodel`、`starlette`；
   - `pyproject.toml` 声明 `pypinyin>=0.50.0`（其余均在 Core 基础依赖内），`requires-python = ">=3.12"`；
   - 62 处 `gsuid_core.*` 导入全部解析通过（0 失效 API）。
3. **AST 红线与外链**：
   - `Any=0`、`cast=0`、`type: ignore=0`、`getattr=0`、`requests=0`、`sync open=0`、裸/宽泛 `except=0`、`active_bot=0`、`target_send=0`；
   - 命中 `py312_only`：`utils/helper/resource_sync.py:133` 使用 `shutil.rmtree(..., onexc=...)`；
   - 外部域名共 9 个：`api.ipify.org`、`httpbin.org`、`klbq.sssysy.top`、`www.dakanu.com`、`wiki.biligame.com`、`web.fanbook.cn`、`open.fanbook.cn`、`api.steamcmd.net`、`shared.akamai.steamstatic.com`。
4. **工具检查**：
   - `ruff check`：0 错误；
   - `basedpyright`（`basic`）：24 个错误（分布在 `dakanu.py`、`model_bind.py`、`player_data.py`、`recent_matches.py`、`match_analysis_services.py`）。

> **关键转折点**：仅凭外链里的 `api.ipify.org` / `httpbin.org` / `klbq.sssysy.top`、Py3.12 命中点、`login_router.py`（Web 路由）与 `analyst_agent.py`（AI 能力代理），静态扫描就立即精准锁定了所有需要深读的高风险模块。

## 9.2 阶段二：排除误报（对照 Core 源码核验）

精读代码并对照 `gsuid_core/` 源码后，主动排除了 4 个常见误报，保证审核报告零水分：

1. `model_bind.py` 出现 `__tablename__` → 实际是在旧表迁移函数中**只读** `CalabiyauBind.__tablename__`，未覆写定义 → **非违规**。
2. 全仓未调用 `convert_img`，直接 `bot.send(image_bytes)` → 核对 `gsuid_core/bot.py` 与 `segment.py`，`convert_message` 对 `bytes` 已原生走 `MessageSegment.image()` 与图床上传 → **非违规**。
3. 4 个 `@scheduler.scheduled_job(..., id=...)` 未写 `replace_existing=True` → 核对 `reload_plugin.py`，新版 Core 热重载会先清理该插件的 scheduler job → **降级为 P1 兼容性建议（非 P0）**。
4. `ruff format --check` 报 91 文件变更 → 查 `git ls-files --eol` 确认仓库索引是 `lf`，仅因 Windows 签出 `crlf` → **非缺陷**。

## 9.3 阶段三：最终分级问题清单

### 🔴 P0 阻断项（4 项，修复后可上架）

| # | 归类 | 位置 | 核心问题 | 修复动作 |
|---|---|---|---|---|
| 1 | Web 安全 / 隐私 | `calabiyau_login/login_services.py:124-190`、`login_router.py` | 未配 `FanbookLoginUrl` 时自动查 `api.ipify.org`/`httpbin.org` 并把明文 `http://<宿主机公网IP>:8765/...` 登录链接与校验码发到群聊，泄露源站 IP 且可被群成员劫持绑定；发码/登录缺服务端限流，路由未走 `/api/calabiyau/...` | 禁止默认探测广播公网 IP（未配域名则提示配置 HTTPS）；登录命令限 `area="DIRECT"`；服务端加发码冷却与失败次数上限；路由迁至 `/api/calabiyau/...` 并改从 `gsuid_core.webconsole.app_app` 导入 `app` |
| 2 | Python 3.11 兼容 | `pyproject.toml:8`、`utils/helper/resource_sync.py:133` | Core 支持 Python 3.11 且不校验 `requires-python`；`shutil.rmtree(..., onexc=...)` 在 3.11 环境直接抛 `TypeError` | 改为兼容 3.11 的 `onexc`/`onerror` 分支，`requires-python` 调整为 `>=3.11` |
| 3 | 资源存储路径 | `utils/utils/paths.py:13-15`、`utils/helper/resource_sync.py` | `RESOURCE_DIR` 指向插件源码目录，启动时直接向源码目录 `git clone` / `rmtree` / `rename`；清单单点依赖个人域名 `klbq.sssysy.top` 且无内置兜底 `maps.json` | 迁至 `DATA_DIR / "resources"` 并登记到 `backup_hints.py`（`regenerable`）；增加 fallback 镜像与最小兜底 `maps.json` |
| 4 | AI 跨插件隔离 | `calabiyau_ai/agents/analyst_agent.py:32-54` | `match_keywords` 含无域泛词 `"战绩分析"` / `"复盘"` / `"上分建议"`，会抢占其他插件路由；`tool_names` 挂了 10 个查询工具但未设 `shared_tool_names`，导致全部被框架从主人格独占剥离 | 删除泛词改为带域限定词；将主人格日常可直调的只读工具加入 `shared_tool_names` |

### 🟡 P1 强烈建议修复（5 项）

1. **类型报错与潜在 `KeyError`**：`basedpyright` 24 处错误，其中 `recent_matches.py:56,87` 直接访问 `NotRequired` 键 `detail["teams"]`。
2. **同步 HTTP 假异步**：`dakanu.py` 共 17 处每次请求新建同步 `httpx.Client` + `@to_thread`，`stats_services.py` 协程内多处直接 `path.read_bytes()`。
3. **长耗时 `to_ai` 缺 `background=True`**：`calabiyau_bind` / `calabiyau_refresh_data` / `calabiyau_replay_download` 耗时数十秒，未配 `background=True` 且 8 处硬编码 `type(bot).__name__ == "MockBot"`。
4. **内存与磁盘无上限增长**：`_locks` / `_room_locks` / `_shop_token_cache` 只增不删；`cache/replay/` 无清理策略；缺 `@on_core_shutdown` 收尾后台任务。
5. **文档与细节不一致**：`README.md:68` 命令写成 `kq战队jmcomic`（实际为 `kq战队查询`）；`help.json:183` 错别字 `"团竟排行"`；Steam 帮助背景热更新后未清理帮助图缓存；未注册 `register_status`；`ruff.toml` 未排除 `pyproject.toml`。

### 🟠 P2 运营合规与上架提示（2 项）

1. 在 README 与商店 `installMsg` 中声明：核心战绩与百科依赖第三方站点（`dakanu.com` / Biligame Wiki），Fanbook 登录涉及手机号、短信验证码与逆向接口，仅建议在配置了 HTTPS 的受信任 Bot 上启用。
2. 核实并更新 README 标注的最低 `gsuid_core` 版本要求（代码依赖 `pytakumi` 新参数、`AgentNode`、`with_read_session` 等较新特性）。

## 9.4 审核尺度示范

- **为何不是「拒绝收录」**：插件整体结构、类型红线、数据库读写分离、订阅体系、`pytakumi` 渲染与 AI 知识库设计都达到了社区高水准，且无任何恶意行为；4 项 P0 均有明确、小成本的修复路径。
- **为何不能「直接上架」**：公网 IP 泄露与群聊登录劫持直接伤害部署者与群友安全，Python 3.11 崩溃与 AI 泛词抢路由会影响商店存量用户与其他插件生态。因此标准结论为：**「修复 4 项 P0 后非常适合收录上架」**。
