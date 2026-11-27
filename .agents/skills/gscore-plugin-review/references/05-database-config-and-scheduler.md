# 五、数据库、配置管理、定时任务、订阅与热重载

> 返回 [SKILL 主入口](../SKILL.md)

本节覆盖持久化数据模型、插件配置项、APScheduler 定时任务、主动推送订阅与生命周期热重载的审核标准。

## 5.1 数据库规范审计

对照 [05-database.md](../../gscore-plugin-development/references/05-database.md) 与 `AGENTS.md §3` 逐项检查：

| 检查项 | 级别 | 规则说明 | 违规反例 |
|--------|------|---------|---------|
| 继承基类与禁止 `__tablename__` | P0 | 必须继承 `BaseIDModel` / `BaseBotIDModel` / `BaseModel`（或 `Bind`/`Push`）；表名自动推导为类名全小写，**禁止定义 `__tablename__ = "..."`** | 手写 `__tablename__ = "my_users"`（注：在迁移逻辑中仅读取 `Model.__tablename__` 不算违规） |
| `__table_args__` 允许重载 | P0 | 表模型须带 `{"extend_existing": True}`（若含约束则放在元组末项） | 热重载插件时 SQLAlchemy 报 `Table '...' is already defined` |
| 读写分离装饰器 | P1 | 纯 `SELECT` 挂 `@with_read_session`；写入/更新/删除挂 `@with_session`；方法须为 `@classmethod` + `async def`，第二参数为 `session: AsyncSession` | 纯查询全部挂 `@with_session`，在 SQLite WAL 模式下占住唯一写闸门 |
| 写闸门超时风险 | **P0** | SQLite 上 `@with_session` 有 **10 秒硬超时**（排队 20 秒抛 `WriteGateTimeout`）；写方法内**严禁**执行 HTTP 请求、`asyncio.sleep` 或大文件读写 | 在 `@with_session` 方法里先发外部 API 再入库，高并发时拖垮全 Core 写锁 |
| 禁止手动 commit / 正确用 `col()` | P1 | 装饰器方法内不要再调用 `await session.commit()`；`where`/`order_by` 字段必须用 `col(cls.field)` 包裹；取 `rowcount` 用 `isinstance(result, CursorResult)` 守卫 | 裸 `cls.id == x` 报类型错；`async_maker()` 漏 `commit()` 或拿它做高频写绕过单写者闸门 |
| SQL 注入防护 | **P0** | 任何原生 `text(...)` 必须使用命名绑定参数（`:param`），禁止 f-string / `%` 拼接用户输入 | `text(f"DELETE FROM t WHERE uid='{uid}'")` |
| Schema 迁移与多库兼容 | P1 | 新增列走 `exec_list`（在 `on_core_start_before` 自动执行）或 `on_core_start_before` 迁移函数；自定义迁移须兼顾 SQLite / MySQL / PostgreSQL | 直接假设底层是 SQLite 路径并操作 `sqlite3.connect(...)` 绕开 SQLModel |
| WebConsole 后台管理 | P1 | 业务绑定/账号表建议用 `@site.register_admin` + `GsAdminModel` 挂载，字段写 `title="..."`；若表含用户敏感凭据（Cookie / Token），审查字段暴露范围与管理权限 | 未加 `extend_existing` 或未填 `title` 导致后台列头难读 |

## 5.2 配置管理审计（`StringConfig` / `CONFIG_DEFAULT`）

对照 [04-config-management.md](../../gscore-plugin-development/references/04-config-management.md) 核查：

1. **路径与实例命名（P0）**：
   - 配置文件必须放 `get_res_path("<PluginName>") / "config.json"`；
   - `StringConfig("<唯一配置名>", CONFIG_PATH, CONFIG_DEFAULT)` 的名称必须唯一且与插件名对应，避免覆盖其他插件配置。
2. **文件/图片上传配置路径安全（P0）**：
   - `GsImageConfig.upload_to`、`GsFileUploadConfig.upload_to`、`GsFilesUploadConfig.data` **必须**使用 `str(get_res_path("<PluginName>") / ...)` 绝对路径，锁定在本插件 `data/` 子目录内，严禁写相对路径或跨插件路径。
3. **敏感配置脱敏（P0）**：
   - 凡涉及第三方 API Key、Webhook Secret、Token、密码的配置项，**必须设置 `secret=True`**，防止在 WebConsole 前端和日志中明文泄露。
4. **废弃类型与取值守卫（P1）**：
   - 禁止使用已废弃的 `GsTimeConfig`，应使用 `GsTimeRConfig`；
   - 读取数值/区间类配置时做上下界校验与兜底（如并发数设置 `max_value`、延迟区间防反向填写），避免用户误填导致死循环或打爆上游。
5. **配置热生效说明（P1）**：
   - 若某项配置仅在模块导入时读取（如把 `GsTimeRConfig` 填进 `@scheduler.scheduled_job` 装饰器参数），配置项 `desc` 中应明确写明「修改后需重启或重载插件生效」（`CalabiyauUID` 的 `FanbookSignTime` 属于正向示范）。

## 5.3 主动推送与订阅审计（`gs_subscribe`）

对照 [06-scheduler-and-subscribe.md](../../gscore-plugin-development/references/06-scheduler-and-subscribe.md) 核查：

- **禁止绕开 `gs_subscribe` 硬推送（P0）**：凡定时通知、签到汇报、公告/兑换码/活动提醒，**严禁**遍历 `gss.active_bot.items()` 硬塞群号或自己维护群号 JSON 推送；通知主人应使用 `send_msg_to_master()`。
- **任务名命名空间隔离（P1）**：`task_name` 全局共享，必须带清晰插件/业务前缀（如 `"订阅卡丘国服兑换码"`），避免与其他插件撞名。
- **`subscribe_type` 与多群隔离（P1）**：
  - 群级广播用 `"session"`；用户级多账号或需按群合并 `@` 时用 `"single"`，注意为同用户在不同群区分作用域（如 `uid=f"group:{ev.group_id}"`），防止退群/多群互覆；
  - 批量推送必须设置发送间隔（如 1.5s），且对单条 `sub.send()` 的网络/超时异常做隔离捕获，防止某一个失效群阻断后续全部订阅者推送。
- **去重与防刷屏（P1）**：定时轮询推送（活动/兑换码/公告）必须把「已推送记录」持久化到数据库，禁止只放内存字典（重启后重复轰炸全部订阅群）；首次开启订阅时，历史已有条目只放回执展示，不触发历史补推。

## 5.4 定时任务、生命周期与热重载安全

对照 [07-lifecycle-hooks.md](../../gscore-plugin-development/references/07-lifecycle-hooks.md) 与 [reload_plugin.py](../../../../gsuid_core/utils/plugins_update/reload_plugin.py) 核查：

| 检查项 | 级别 | 核查要点 |
|--------|------|---------|
| 启动阶段归位 | P0 | 耗时网络同步、资源下载、AI 知识库同步放 `@on_core_start`（后台异步），**禁止**放 `@on_core_start_before` 或模块顶层阻塞 WS 启动；表结构迁移放 `@on_core_start_before` |
| 定时任务异常处理 | P1 | APScheduler 内部异常会被吞掉，任务函数需捕获业务/网络异常并打 `logger.warning` / `logger.exception`；并发触发加 `asyncio.Lock` 防重入 |
| 固定 `id` 定时任务 | P1 | `@scheduler.scheduled_job(..., id="...")` 与 `scheduler.add_job(..., id="...")` 统一加 `replace_existing=True`（虽然新版 Core `reload_plugin` 会先清理旧 job，加上后可兼容旧 Core 与重复注册） |
| `gss.on_bot_connect` | P1 | 仅在确实依赖 Bot 上线触发时使用；注意重连频繁触发会刷屏，需做节流/去重并自捕异常 |
| 停机收尾 `@on_core_shutdown` | P1 | 若插件维护后台 `asyncio.create_task`、内存待刷盘缓冲或长连接 `httpx.AsyncClient`，必须注册 `@on_core_shutdown` 并在 `asyncio.wait_for(..., timeout=5)` 内有序关闭 |
| 热重载残留检查 | P1 | 确认插件未修改 Core 内部全局私有对象（导致 `reload_plugin` 无法摘除）；Web 路由、Admin 表、Backup hints、Help 注册名与插件目录名保持一致 |
