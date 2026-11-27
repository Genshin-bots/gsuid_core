# 四、数据存储、运行时资源、缓存与备份

> 返回 [SKILL 主入口](../SKILL.md)

插件上架商店后会在 Docker 挂载、Windows/Linux 混合环境、高频大群与多次热更新场景下长期运行。本节核查文件落盘边界、资源分发可靠性、内存/磁盘防膨胀与备份规范。

## 4.1 路径隔离红线（`get_res_path`）

对照 [01-plugin-basics.md](../../gscore-plugin-development/references/01-plugin-basics.md) §1.5 与 [16-common-utilities.md](../../gscore-plugin-development/references/16-common-utilities.md) §16.1：

| 检查项 | 级别 | 规则 | 违规后果 |
|--------|------|------|---------|
| 禁止写插件源码目录 | **P0** | 所有运行时配置、用户快照、缓存、下载素材与外部资源仓必须落在 `get_res_path("<PluginName>")`（即 `data/<PluginName>/`） | Docker 只读代码挂载直接报错；插件卸载重装丢失数据；嵌套 `.git` 仓污染源码树与更新流程 |
| 禁止写 `Path.cwd()` 或系统临时目录 | **P0** | 禁止相对路径落盘（如 `open("cache.json", "w")`）或把持久数据丢进 `/tmp` | 启动工作目录变化导致数据丢失或误写到 Core 根目录 |
| 禁止跨插件读写目录 | **P0** | 不得读写其他插件的 `data/<OtherPlugin>/` 目录 | 破坏插件隔离，越权读取或损坏他方数据 |
| 路径集中管理 | P1 | 推荐统一在 `utils/paths.py` 或 `RESOURCE_PATH.py` 声明全部路径常量 | 路径散落各文件，备份与迁移易遗漏 |
| 文件名与路径穿越防护 | **P0** | 凡由用户输入或外部 JSON 拼接路径（如玩家 ID、roomId、知识库 `rel_path`），必须校验纯数字/白名单字符，或做 `resolved.is_relative_to(base_dir.resolve())` 检查 | `../` 路径穿越读取/覆盖宿主任意文件 |

**真实案例（`CalabiyauUID`）**：
- **正向**：[player_data.py:39-42](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/utils/helper/player_data.py#L39-L42) 对 `player_id` 强制 `isdigit()` 并使用 `.tmp` + `replace` 原子写入；[entities.py:321-325](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/calabiyau_ai/knowledge/entities.py#L321-L325) 的 `_resolve_safe_path()` 校验了相对路径不逃逸知识目录。
- **P0 违规**：[paths.py:13-15](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/utils/utils/paths.py#L13-L15) 将 `RESOURCE_DIR` 定义为 `Path(__file__).resolve().parents[1] / "resources"`（落在 `plugins/CalabiyauUID/CalabiyauUID/utils/resources`），[resource_sync.py](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/utils/helper/resource_sync.py) 启动时直接向源码目录执行 `git clone` / `rmtree` / `rename`。正确做法是迁至 `DATA_DIR / "resources"`。

## 4.2 外部资源同步与容灾能力

若插件在启动或运行时从远端拉取素材/配置/知识包，必须核查：

1. **禁止阻塞 Core 启动（P0）**：资源下载/同步必须放在 `@on_core_start`（后台异步）而非模块顶层 import 或 `@on_core_start_before`，且网络/git 失败时仅记录 warning，绝不能导致 Core 启动崩溃。
2. **避免单点失效（P0/P1）**：
   - 检查是否仅硬编码作者个人域名或单一私有链接（如 `CalabiyauUID` 仅写 `https://klbq.sssysy.top/resource_mirror.json`，且清单内仅 1 个 CNB 源，无 fallback）；
   - 核心映射表（如角色 ID / 地图字典）建议随插件内置最小兜底版本，保证断网或镜像不可达时核心命令仍可降级运行。
3. **验证下载源与文件完整性（P1）**：
   - 远端 manifest/URL 解析须限定协议为 `https://`（必要时校验域名白名单），避免清单被劫持后引导下载恶意地址或触发 SSRF；
   - 图片/文件写入采用临时文件校验后再 `os.replace`，防止半截损坏文件永久留在缓存目录。

## 4.3 内存与磁盘资源治理

商店插件常部署在多群、多用户、7×24 小时运行的公用 Bot 上，必须审查无界增长点：

| 隐患类型 | 级别 | 审查方法 | 修复方向 |
|----------|------|---------|---------|
| 无界模块级字典（锁/Token/缓存） | P1 | 扫模块顶层 `dict[str, asyncio.Lock]`、`dict[str, ...]`（如按 `player_id` / `room_id` / `user_id` 建键但永不删除） | 使用 `WeakValueDictionary`（锁）、带 `maxsize` 的 `OrderedDict`/LRU+TTL 缓存（参考 `CalabiyauUID` 的 `AsyncTtlCache`） |
| 磁盘缓存无上限/无清理 | P1 | 检查 `img_cache/`、`replay/`、下载临时文件是否只有写入没有过期淘汰或容量上限 | 加定时清理任务（按 mtime 或总字节数 LRU 淘汰） |
| 后台 `asyncio.Task` 泄露 | P1 | 检查 `asyncio.create_task()` 是否保存强引用、是否有 `done_callback` 消费异常、是否随 `@on_core_shutdown` 取消等待 | 维护 `_background_tasks` 集合并在 `on_core_shutdown` 设超时收尾 |

**真实案例（`CalabiyauUID`）**：
- `match_cache.py` 规范实现了带 `maxsize` 和 `ttl_seconds` 的 `AsyncTtlCache`，但 `refresh.py:39`（`_locks`）、`replay_services.py:18`（`_room_locks`）、`fanbook_sign.py:40`（`_shop_token_cache`）均为只增不删字典；`cache/replay/` 回放文件单份体积较大且无定期清理策略。

## 4.4 备份路径登记（`register_backup_path`）

对照 [16-common-utilities.md](../../gscore-plugin-development/references/16-common-utilities.md) §16.10 核查：

- `plugin` 参数是否与 `Plugins(name=...)` **完全一致**（不一致会导致热重载残留）；
- `kind` 分类是否准确：
  - `user_data`：用户不可再生数据（存档、快照、绑定导出）；
  - `config`：配置文件、用户自定义背景图（通常 `default_selected=True`）；
  - `regenerable`：可重新下载的素材仓（**不要**把纯临时缓存目录如 `IMAGE_TEMP` 标成默认勾选）；
- 所有登记路径必须落在 `data/` 内部（越界会抛 `ValueError`）；
- 为兼容旧版 Core，导入 `gsuid_core.utils.backup.hints` 时允许捕获 `ImportError` 跳过。
