# 八、代码红线、类型安全与异步 I/O 审计

> 返回 [SKILL 主入口](../SKILL.md)

本节对照 [17-code-redlines.md](../../gscore-plugin-development/references/17-code-redlines.md) 与 [`AGENTS.md`](../../../AGENTS.md) §1–§4、§8，审查插件的代码质量、静态类型健康度与异步并发表现。

## 8.1 代码规范红线核查表

| 红线项 | 级别 | 审查口径 | 合法例外（不得误报） |
|--------|------|---------|---------------------|
| `try/except` 兜底吞错 | P1（严重吞错升 P0） | 禁止用裸 `except:`、宽泛 `except Exception:` 掩盖内部类型/属性/逻辑错误 | ① 解析不可信外部输入（网络 `httpx.HTTPError`、第三方 JSON/图片解码）；② `_ai_return_xxx()` 观测辅助函数；③ APScheduler 定时任务 / `on_bot_connect` 顶层防吞错日志；④ 可选新 Core 模块（如 `backup.hints`）的 `ImportError` |
| `cast()` 强制类型欺骗 | P1 | 禁止使用 `typing.cast()` 绕过类型检查，应使用 `Union` + `isinstance` 守卫 | 无 |
| `type: ignore` 掩盖自身错误 | P1 | 严禁用 `# type: ignore` 掩盖自身代码类型漏洞 | 仅第三方库类型存根确有缺陷且无法绕过时（如官方示例中极少数第三方 `PageSchema` 标注问题） |
| `getattr` / `dict.get` 兜底 | P1 | 内部结构必须用 `TypedDict` / `@dataclass` 明确定义，用 `in` / `isinstance` 守卫后直接访问 | 解析外部非结构化 JSON 可先做集中校验函数（如 `_read_str_field`），不建议在业务层到处 `d.get("a", {}).get("b")` |
| `Any` 传播 | P1 | 禁止引入 `Any`（含 `dict[str, Any]`、`list[Any]`、`Callable[..., Any]`） | 调用第三方无类型返回时必须在边界立即用 `isinstance` 收窄 |
| 同步阻塞网络/文件库 | P0/P1 | 禁止 `requests`、`urllib.request`、协程内 `time.sleep`、协程内同步 `open()` 大文件读写 | CPU 密集计算/PIL 图像处理可用 `@to_thread` 桥接 |

## 8.2 `basedpyright` 报错归类（识别真实运行时 Bug）

在 Core 环境下以 `basic` 模式对插件跑 `basedpyright` 后，不能只看报错总数，必须拆出**会引发真实线上崩溃的类型错误**：

1. **高危真 Bug（优先修，P1/P0）**：
   - `reportTypedDictNotRequiredAccess`：直接下标访问 `NotRequired[...]` 键（如 `detail["teams"]`），一旦上游接口省略该字段，运行时立刻抛 `KeyError`；
   - `reportOptionalMemberAccess` / `reportOptionalSubscript`：未判 `None` 直接调属性或下标；
   - `reportCallIssue` / `reportArgumentType` 指向框架 API 参数不匹配（说明调用了过时或错误的 Core 函数签名）。
2. **中危类型漏洞（P1）**：
   - 把 `dict[Unknown, Unknown]` 直接赋给 `TypedDict`（跳过字段校验，外部缓存 JSON 损坏时会在深层业务随机炸开）；
   - `TypeGuard` / `object` 索引未收窄、SQLAlchemy `Row` 与 `tuple` 类型不匹配、Pillow `Image.point(lambda ...)` 签名不兼容。

**真实案例（`CalabiyauUID`）**：
- `CalabiyauUID` 全仓做到了 0 `Any` / 0 `cast` / 0 `type: ignore` / 0 `getattr` / 0 `requests`，工程纪律非常出色；
- 但 `basedpyright` 仍检出 24 处错误，其中 [recent_matches.py#L56](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/utils/helper/recent_matches.py#L56) 与 `#L87` 对 `NotRequired` 字段 `detail["teams"]` 直接下标取值，属于真实潜在 `KeyError`（只需改为 `detail["teams"] if "teams" in detail else []` 即可消除）。

## 8.3 全链路异步与并发健康度审计

1. **警惕「同步 HTTP 客户端 + `@to_thread`」假异步（P1）**：
   - [16-common-utilities.md](../../gscore-plugin-development/references/16-common-utilities.md) §16.7 明确指出：`@to_thread` 适用于 CPU 密集型或无法异步化的同步库（PIL、数据分析），**不适用于网络 I/O**。
   - **真实案例（`CalabiyauUID`）**：[dakanu.py](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/utils/api/dakanu.py) 全文 17 处每次请求新建 `httpx.Client()` 并套 `@to_thread`（0 处 `httpx.AsyncClient`），高并发下既消耗线程池又无法复用 TCP/TLS 连接。应统一改用 `httpx.AsyncClient`（建议模块级连接池 + `@on_core_shutdown` 关闭）。
2. **协程内隐藏的同步 I/O（P1）**：
   - 排查 `async def` 内部直接调用 `Path.read_bytes()`、`Path.read_text()`、`Path.write_text()`、`shutil.copyfile()`、`Image.open()`（例如 `CalabiyauUID` 的 [stats_services.py#L176-L223](https://github.com/sssysy/CalabiyauUID/blob/fbc30b0/CalabiyauUID/calabiyau_stats/stats_services.py#L176-L223) 在循环内直接 `path.read_bytes()`）；批量或大文件读写应走 `aiofiles` 或 `@to_thread`。
3. **并发控制与限流（P1）**：
   - 批量请求外部接口（如全员自动签到、批量拉取近期对局详情）必须用 `asyncio.Semaphore` 限制并发数，并对高频查询接口做 TTL 缓存或同键合并（in-flight dedup），避免把上游打成 429。

## 8.4 `Bot` / `_Bot` / `MockBot` 耦合审计

- **`_Bot` 隔离（P0）**：插件业务代码严禁直接 import 或实例化 `gsuid_core.bot._Bot`（见 `AGENTS.md §8`）。
- **`MockBot` 耦合（P1）**：若业务代码大量写 `type(bot).__name__ == "MockBot"` 来分支控制提示消息或异步任务（例如 `CalabiyauUID` 中出现 8 处），说明对 AI 与用户的长耗时路径做了手工分叉；应评估用框架原生 `background=True`（SKILL §10.6）替代部分手工分叉，降低对内部类名的耦合。
- **插件日志规范（P2）**：统一使用 `from gsuid_core.logger import logger`，带清晰插件前缀（如 `[卡拉彼丘·模块]`），不用裸 `print()`。
