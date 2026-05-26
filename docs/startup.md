# GsCore 项目启动与插件载入

## 一、项目启动流程

### 1.1 启动入口

```
python -m gsuid_core
    │
    ▼
asyncio.run(main())
    │
    ▼
core.py::main()
```

### 1.1.1 启动入口源码

```python
# gsuid_core/core.py::main()

async def main():
    # 1. 解析 CLI 参数：--dev / --port / --host
    # 2. 切换 HuggingFace 镜像源（os.environ["HF_ENDPOINT"]）
    # 3. await init_database()        # 创建 SQLite + 初始化基础表
    # 4. await load_gss(args.dev)     # 加载所有插件 + 触发 @sv 装饰器
    # 5. import gsuid_core.ai_core.startup   # 注册唯一 init_ai_core 钩子
    # 6. 构造 FastAPI app（在 app_life.lifespan 中跑两阶段 hook）
    # 7. 注册 @app.websocket("/ws/{bot_id}") + 可选 @app.post("/api/send_msg")
    # 8. 装 SIGINT/SIGTERM 信号处理（Windows 无 add_signal_handler，依赖 uvicorn）
    # 9. uvicorn.Server(config).serve()  # 由 lifespan 在内部跑启动 hook
```

### 1.2 启动时序图

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              项目启动时序图                                    │
└─────────────────────────────────────────────────────────────────────────────────┘

     main()                    数据库                    插件加载              Web服务
       │                          │                        │                     │
       │  asyncio.run(main)       │                        │                     │
       │─────────────────────────►│                        │                     │
       │                          │                        │                     │
       │                          │ init_database()        │                     │
       │                          │────────────────────────►│                     │
       │                          │                        │                     │
       │                          │                      加载完成               │
       │                          │◄────────────────────────│                     │
       │                          │                        │                     │
       │                          │                        │ load_gss(dev)       │
       │                          │                        │────────────────────►│
       │                          │                        │                     │
       │                          │                        │                遍历插件目录
       │                          │                        │                     │
       │                          │                        │                解析 pyproject.toml
       │                          │                        │                     │
       │                          │                        │                合并安装缺失依赖
       │                          │                        │                (一次性 pip 调用)
       │                          │                        │                     │
       │                          │                        │                cached_import()
       │                          │                        │                     │
       │                          │                        │              注册 @sv.on_xxx
       │                          │                        │                     │
       │                          │                        │◄────────────────────│
       │                          │                        │                     │
       │                          │                        │                  加载完成
       │                          │                        │                     │
       │                       import gsuid_core.ai_core.startup                 │
       │                       (注册唯一 @on_core_start 钩子 init_ai_core)        │
       │                          │                        │                     │
       │  ┌─────────────────────────────────────────────────────────────────────┐│
       │  │ 阶段一: core_start_before_execute() [阻塞式, 按优先级分组并发]      ││
       │  │   ├── move_database()              priority=-100                    ││
       │  │   ├── create_core_tables()         priority=-90                     ││
       │  │   ├── trans_adapter()              priority=-80                     ││
       │  │   └── load_global_val()            priority=0                       ││
       │  └─────────────────────────────────────────────────────────────────────┘│
       │                          │                        │                     │
       │                    ┌─────┴─────┐                  │                     │
       │                    │ 启动 uvicorn │                │                     │
       │                    │ WebSocket   │                │                     │
       │                    │ HTTP (可选) │                │                     │
       │                    └─────┬─────┘                  │                     │
       │                          │                        │                     │
       │◄─────────────────────────│                        │                     │
       │     服务已启动            │                        │                     │
       │                          │                        │                     │
       │  ┌─────────────────────────────────────────────────────────────────────┐│
       │  │ 阶段二: core_start_execute() [后台异步, asyncio.create_task]         ││
       │  │   ├── _start_rm_cleanup (priority=10) - RM 资源清理 worker          ││
       │  │   └── init_ai_core (priority=0)  AI 核心一站式初始化                ││
       │  │         ├── to_thread(_import_ai_heavy_deps)                        ││
       │  │         │     - handle_ai / buildin_tools 等重依赖延迟导入          ││
       │  │         ├── _init_rag         (RAG / Embedding / Qdrant collection)││
       │  │         ├── _init_persona     (默认 Persona「早柚」+ 老配置迁移)   ││
       │  │         ├── _init_scheduled_task (重新加载 pending 定时任务)        ││
       │  │         ├── _init_planning    (Kanban 任务编排层)                   ││
       │  │         ├── _init_memory      (双路检索 + IngestionWorker)          ││
       │  │         ├── _init_mcp_tools   (MCP 工具自动注册)                    ││
       │  │         ├── _init_meme       (表情包打标 worker)                    ││
       │  │         ├── _init_statistics (AI 统计 + Heartbeat 巡检)            ││
       │  │         └── _init_mcp_server  (MCP Server 对外提供工具)             ││
       │  └─────────────────────────────────────────────────────────────────────┘│
       │                          │                        │                     │
       ▼                          ▼                        ▼                     ▼
```

> **重要变更**：启动钩子分为两个阶段，且 AI 子系统已**统一收敛到 `ai_core/startup.py`**：
> 1. **`on_core_start_before`** — 在 WS 服务启动**之前**阻塞执行，用于数据库迁移、
>    表创建、Schema 升级、全局变量加载等必须在连接建立前完成的操作。
> 2. **`on_core_start`** — 在 WS 服务启动**之后**作为后台任务异步执行，**不阻塞连接**。
>    AI 子系统不再各自 `@on_core_start`，而是由 `gsuid_core/ai_core/startup.py`
>    注册唯一一个 `init_ai_core()` 钩子，按 `_INIT_STEPS` 顺序串行初始化各模块；
>    `sklearn` / `sentence-transformers` / `buildin_tools` 等 CPU 重依赖通过
>    `asyncio.to_thread(_import_ai_heavy_deps)` 在独立线程导入，避免冻住 loop。

---

## 二、核心初始化详解

### 2.1 数据库初始化

```python
# gsuid_core/core.py::main()

from gsuid_core.utils.database.base_models import init_database

await init_database()
```

```python
# gsuid_core/utils/database/base_models.py

async def init_database():
    # 1. 创建 SQLModel 数据库引擎
    # 2. 创建所有表（如果不存在）
    # 3. 执行数据库迁移
```

### 2.2 插件加载

```python
# gsuid_core/gss.py

gss = GsServer()  # 单例

async def load_gss(dev_mode: bool = False):
    if not gss.is_load:
        gss.is_load = True
        await gss.load_plugins(dev_mode)
        remove_repeat_job()
```

```python
# gsuid_core/server.py::GsServer.load_plugins()

async def load_plugins(self, dev_mode: bool = False):
    refresh_installed_dependencies()

    # 1. 把仓库根目录加入 sys.path，让插件可以 from gsuid_core ...
    root_path = str(Path(__file__).parents[1])
    if root_path not in sys.path:
        sys.path.append(root_path)

    plug_path_list = [
        p for p in list(BUILDIN_PLUGIN_PATH.iterdir()) + list(PLUGIN_PATH.iterdir())
        if p.is_dir() or (p.is_file() and p.suffix == ".py")
    ]

    # 阶段一：发现插件 + 收集缺失依赖（不立即 pip）
    all_plugins: List[Tuple[str, Path, str]] = []
    for plugin in plug_path_list:
        if dev_mode and not plugin.name.endswith("-dev"):
            continue
        d = self.load_plugin(plugin, dev_mode)
        if isinstance(d, str):
            continue
        all_plugins.extend(d)

    # 阶段二：合并所有插件收集到的缺失依赖，一次 pip 调用（含镜像源 fallback）
    flush_pending_installs()

    # 阶段三：依次 import 模块；模块级 @sv.on_xxx / @ai_tools / ai_entity 在此触发
    _import_durations.clear()
    for module_name, filepath, _type in all_plugins:
        try:
            self.cached_import(module_name, filepath, _type)
        except Exception as e:
            logger.exception(f"❌ 插件{filepath.stem}导入失败")

    # 阶段四：把每个插件配置 dump 回独立 JSON + 重写 config.json
    plugin_config_store.save_all()
    core_config.lazy_write_config()
```

> `core_start_def` 集合不再在 `load_plugins()` 内部触发，而是由 `app_life.lifespan`
> 在 WS 启动后用 `asyncio.create_task(core_start_execute())` 跑。
> 同样的，**`flush_pending_installs()` 把所有插件的缺失依赖合并成一次 pip 调用**，
> 解决早期"每个插件单独跑 pip + 镜像源 fallback"导致首次启动巨慢的问题。

---

## 三、插件发现与分类

### 3.1 插件目录结构

```
gsuid_core/
├── plugins/                    # 用户插件目录
│   ├── plugin_a/
│   │   └── __init__.py         # 单插件包
│   ├── plugin_b/
│   │   └── __full__.py         # 全量加载模式
│   │       ├── module_a.py
│   │       └── module_b.py
│   └── single_plugin.py        # 单文件插件
│
└── buildin_plugins/            # 内置插件目录
    └── core_command/
        └── __init__.py
```

### 3.2 插件加载类型

| 文件存在 | 加载方式 | 说明 |
|----------|----------|------|
| `__init__.py` | **单插件包** | 文件夹作为单个插件包加载 |
| `__full__.py` | **全量加载** | 加载文件夹内所有 `.py` 模块 |
| `__nest__.py` | **嵌套加载** | 嵌套模式加载 |
| `*.py` (单文件) | **单文件插件** | 直接作为插件导入 |

### 3.3 加载类型判定

```python
# gsuid_core/server.py::load_plugin()

def load_plugin(self, plugin: Union[str, Path], dev_mode: bool = False):
    if plugin.is_dir():
        plugin_path = plugin / "__init__.py"
        plugins_path = plugin / "__full__.py"
        nest_path = plugin / "__nest__.py"
        src_path = plugin / plugin.stem  # plugin_name/plugin_name.py

        if plugins_path.exists():
            # 全量加载模式
            module_list = load_dir_plugins(..., __full__.py found)
        elif nest_path.exists() or src_path.exists():
            # 嵌套加载模式
            module_list = load_dir_plugins(..., nest=True)
        elif plugin_path.exists():
            # 单插件包模式
            module_list = [(f"{plugin_parent}.{plugin.name}.__init__", plugin_path, "plugin")]
    else:
        # 单文件插件
        module_list = [(f"{plugin_parent}.{plugin.name[:-3]}", plugin, "single")]
```

---

## 四、依赖管理

### 4.1 pyproject.toml 解析

```python
# gsuid_core/server.py::check_pyproject()

def check_pyproject(pyproject: Path):
    # 支持两种格式:
    # 1. PEP 621 (project 表)
    # 2. Poetry (tool.poetry 表)

    if "project" in toml_data:
        dependencies = toml_data["project"].get("dependencies", [])
        # 处理 gscore_auto_update_dep 特殊依赖
    elif "tool" in toml_data and "poetry" in toml_data["tool"]:
        dependencies = toml_data["tool"]["poetry"].get("dependencies", {})
```

### 4.2 依赖安装流程

```
发现依赖
    │
    ▼
normalize_name() 规范化名称
(统一小写，-_. 互换)
    │
    ├─── 在 ignore_dep 列表中? ──► 跳过
    │       (fastapi/pydantic/gsuid-core/toml/packaging等基础包)
    │
    ├─── 未安装? ──► 加入安装队列
    │
    └─── 已安装但版本不符? ──► 检查是否需要更新
                                │
                                ▼
                           install_packages()
                                │
                                ▼
                           依次尝试镜像源:
                           字节 → 阿里 → 清华 → PyPI
                                │
                                ▼
                           安装成功 / 全部失败
```

### 4.3 镜像源配置

```python
mirrors = [
    ("字节源 (Volces)", "https://mirrors.volces.com/pypi/simple/"),
    ("阿里源 (Aliyun)", "https://mirrors.aliyun.com/pypi/simple/"),
    ("清华源 (Tsinghua)", "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ("官方源 (PyPI)", "https://pypi.org/simple"),
]
```

### 4.4 忽略的基础依赖

```python
ignore_dep = {
    "python",
    "fastapi",
    "pydantic",
    "gsuid-core",
    "toml",
    "packaging",
}
```

---

## 五、模块导入机制

### 5.1 cached_import 流程

```python
# gsuid_core/server.py::cached_import()

def cached_import(self, module_name: str, filepath: Path, _type: str):
    # 1. 检查缓存
    if module_name in _module_cache:
        return _module_cache[module_name]

    # 2. 创建模块规范
    spec = importlib.util.spec_from_file_location(module_name, filepath)

    # 3. 创建模块
    module = importlib.module_from_spec(spec)

    # 4. 先放入 sys.modules (处理循环导入)
    sys.modules[module_name] = module

    # 5. 执行模块
    try:
        spec.loader.exec_module(module)
    except Exception:
        # 加载失败，清理 dirty module
        del sys.modules[module_name]
        raise

    # 6. 写入缓存
    _module_cache[module_name] = module

    # 7. 触发装饰器注册
    # @sv.on_command 等装饰器在此时执行
```

### 5.2 循环导入处理

```
A 模块导入
    │
    ▼
sys.modules["A"] = placeholder_module
    │
    ▼
执行 A 模块代码
    │
    ├──► 导入 B 模块
    │         │
    │         ▼
    │    sys.modules["B"] = placeholder
    │         │
    │         ▼
    │    B 模块执行完毕
    │         │
    │         ▼
    │    B 对象可用
    │
    ▼
A 模块执行完毕
    │
    ▼
A 对象可用
```

---

## 六、配置填充机制

### 6.1 核心配置结构

```python
# gsuid_core/config.py::CONFIG_DEFAULT

CONFIG_DEFAULT = {
    "HOST": "localhost",
    "PORT": "8765",
    "ENABLE_HTTP": False,
    "WS_TOKEN": "",
    "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"],
    "masters": [],
    "superusers": [],
    "REGISTER_CODE": _generate_register_code(),
    "misfire_grace_time": 90,
    "log": {
        "level": "INFO",
        "output": ["stdout", "stderr", "file"],
        "module": False,
    },
    "enable_empty_start": True,
    "command_start": [],
    "buffered_user_writes": False,
    "sv": {},      # SV 配置（保留键，运行时使用）
}
```

> 旧版 `OLD_CONFIG_PATH = gsuid_core/config.json`（仓库内）启动时若存在，
> 会被 `shutil.copy2` 拷贝到 `data/config.json` 并删除源文件（一次性迁移）。

> **Breaking Change**: `plugins` key 已从 `config.json` 中移除。
> 每个插件的配置现在独立存储在 `data/plugins_configs/<plugin_name>.json` 中。

### 6.2 配置加载流程

```
CoreConfig.__init__()
    │
    ├──► 检查是否存在旧配置文件
    │         │
    │         ▼
    │    shutil.copy2(OLD_CONFIG_PATH, CONFIG_PATH)
    │
    ├──► 不存在则创建默认配置
    │         │
    │         ▼
    │    json.dump(CONFIG_DEFAULT, file)
    │
    └──► update_config()
              │
              ▼
         读取 config.json
              │
              ▼
         合并默认配置（填补缺失项）
              │
              ▼
         core_config.config = merged_config
```

### 6.3 插件配置存储（PluginConfigStore）

```python
# gsuid_core/config.py::PluginConfigStore

class PluginConfigStore:
    """插件独立配置存储

    每个插件的配置存储为 data/plugins_configs/<plugin_name>.json，
    替代原先 config.json["plugins"] 的大字典模式。
    """

    def __init__(self):
        self._dirty: Set[str] = set()
        self._cache: Dict[str, dict] = {}
        self._migrate_from_config()  # 启动时迁移旧配置
        self._load_all()             # 加载所有插件配置到内存

    def _migrate_from_config(self):
        """启动时检查 config.json 中是否存在 plugins key，
        如果存在则将每个插件拆分为独立 JSON 文件，然后移除该 key。"""

    def get_all(self) -> Dict[str, dict]:
        """返回所有插件配置的引用（与旧 config_plugins 兼容）。"""

    def save(self, plugin_name: str) -> None:
        """持久化单个插件配置到文件。"""

    def save_all(self) -> None:
        """持久化所有插件配置。"""
```

### 6.4 启动时迁移流程

```
PluginConfigStore.__init__()
    │
    ├──► _migrate_from_config()
    │         │
    │         ├── config.json 中存在 "plugins" key?
    │         │         │
    │         │         ▼
    │         │    备份 config.json → data/config_backup.json（如不存在）
    │         │         │
    │         │         ▼
    │         │    遍历每个插件配置
    │         │         │
    │         │         ▼
    │         │    写入 data/plugins_configs/<name>.json
    │         │         │
    │         │         ▼
    │         │    从 config.json 移除 "plugins" key
    │         │
    │         └── 不存在则跳过
    │
    └──► _load_all()
              │
              ▼
         遍历 data/plugins_configs/*.json
              │
              ▼
         加载到内存缓存 self._cache
```

### 6.5 插件配置注册

```python
# gsuid_core/sv.py::Plugins

class Plugins:
    def __new__(cls, name: str, ...):
        # 单例模式
        if name in SL.plugins:
            return SL.plugins[name]
        else:
            _plugin = super().__new__(cls)
            SL.plugins[name] = _plugin
            return _plugin

    def __init__(self, name: str = "", ...):
        if name not in config_plugins:
            # 首次创建，从样本复制
            _plugins_config = deepcopy(plugins_sample)
            _plugins_config["name"] = name
            config_plugins[name] = _plugins_config
            # 持久化到独立 JSON 文件
            plugin_config_store.save(name)
```

### 6.6 SV 配置注册

```python
# gsuid_core/sv.py::SV

class SV:
    def __init__(self, name: str = "", ...):
        # 从 config_plugins（PluginConfigStore 缓存）获取插件配置
        plugin_config = config_plugins[self.self_plugin_name]

        # 设置 SV 级别的配置
        self.sv = plugin_config.get("sv", {}).get(name, {})
```

---

## 七、Core Start 钩子系统

### 7.1 钩子定义

```python
# gsuid_core/server.py

core_start_def: Set[_DefHook] = set()
core_start_before_def: Set[_DefHook] = set()
core_shutdown_def: Set[_DefHook] = set()

def on_core_start_before(func=None, /, priority: int = 0):
    """注册在 WS 服务启动之前执行的钩子函数。

    用于数据库迁移、全局变量加载等必须在连接建立前完成的操作。
    与 on_core_start 不同，此钩子会阻塞 WS 服务启动，确保执行完毕后才开始接受连接。
    """
    ...

def on_core_start(func=None, /, priority: int = 0):
    """Core启动时执行的钩子（后台异步，不阻塞 WS 服务启动）"""
    ...

def on_core_shutdown(func=None, /, priority: int = 0):
    """Core关闭时执行的钩子"""
    ...
```

### 7.2 启动前钩子（`on_core_start_before`）

> 在 WS 服务启动**之前**阻塞执行，按优先级**升序**分组，**同优先级并发** `gather`，
> 必须全部完成后才开始接受连接。优先级数值越小越先执行。

| 钩子函数 | 模块 | 优先级 | 功能 |
|----------|------|--------|------|
| `move_database` | `utils/database/startup.py` | -100 | 旧版 `GsData.db` 迁移到新版路径 + 清理 `global_val/*.json` |
| `create_core_tables` | `utils/database/startup.py` | -90 | 创建核心表（含 AI 模型注册；AI 总开关关闭时跳过 AI 表） |
| `trans_adapter` | `utils/database/startup.py` | -80 | 执行 ALTER / CREATE INDEX 等 Schema 升级语句 |
| `load_global_val` | `buildin_plugins/core_command/core_status/command_global_val.py` | 0 | 加载 Bot 流量统计、Bot 最大 QPS 配置 |

### 7.3 启动后钩子（`on_core_start`）

> 在 WS 服务启动**之后**作为后台任务异步执行 (`asyncio.create_task`)，不阻塞连接。
> AI 子系统不再各自注册 `@on_core_start`——统一由 `init_ai_core()` 顺序执行。

| 钩子函数 | 模块 | 优先级 | 功能 |
|----------|------|--------|------|
| `_start_rm_cleanup` | `utils/resource_manager.py` | 10 | 启动 ResourceManager 资源清理后台任务（图片/文件 RM） |
| `init_ai_core` | `ai_core/startup.py` | 0 | AI 核心统一入口；按 `_INIT_STEPS` 顺序串行触发各 AI 子系统初始化 |

> 插件可以继续使用 `@on_core_start(priority=N)` 注册自己的启动钩子；
> 同优先级会与 `init_ai_core` 并发执行。

### 7.4 `init_ai_core` 子系统列表（`_INIT_STEPS`）

`init_ai_core()` 首先把 AI 重依赖通过 `asyncio.to_thread(_import_ai_heavy_deps)`
放到独立线程做同步 import（包括 `handle_ai` / `buildin_tools` 等会拉
`sklearn` / `sentence-transformers` 的模块），然后按顺序串行执行下列子步骤——
单步异常被 `try/except` 兜住并继续下一步：

| # | 名称 | 实现 | 主要工作 |
|---|------|------|----------|
| 1 | RAG | `ai_core/rag/startup.py::init_all()` | `pre_download_models` → 线程内 `init_embedding_model` → 建 tools / knowledge / image collection → `sync_tools(get_all_tools())` + `sync_knowledge` + `sync_images` |
| 2 | Persona | `ai_core/persona/startup.py::init_default_personas()` | 写入「早柚」默认 Persona，迁移 voice_anchor 旁路字段 |
| 3 | 定时任务 | `ai_core/scheduled_task/startup.py::init_scheduled_tasks()` | `reload_pending_tasks()` 把 DB 中 pending 任务挂回 APScheduler |
| 4 | 长任务编排 | `ai_core/planning/startup.py::init_planning()` | 注册 Kanban LLM 工具 / 内置 + 用户 Capability 画像 / 评估代理；启动期僵尸子任务恢复 + 周期模板挂载 + 每日 04:00 Artifact TTL 清理 |
| 5 | Memory | `ai_core/memory/startup.py::init_memory_system()` | Qdrant Collection 检查 → `IngestionWorker` 独立线程 → 多模态摄入 worker → 每周一次记忆生命周期维护 job |
| 6 | MCP 工具 | `ai_core/mcp/startup.py::init_mcp_tools()` | 读取 `mcp_configs/*.json` → 连接 enabled 服务器 → 把每个工具动态注册到 `_TOOL_REGISTRY["mcp"]`（仅当 `register_as_ai_tools=True`） |
| 7 | Meme | `ai_core/meme/startup.py::init_meme_module()` | 创建 inbox/common/rejected 目录 → 表情包 Qdrant Collection → 打标 worker → 回灌 inbox 遗留记录 |
| 8 | 统计 | `ai_core/statistics/startup.py::init_ai_core_statistics()` | `AISessionRegistry` 空闲清理循环 + `start_heartbeat_inspector` 巡检 + 当日数据回灌 |
| 9 | MCP Server | `ai_core/mcp/server.py::init_mcp_server()` | 把已注册的触发器/工具通过 MCP 协议对外暴露（独立 task） |

> **AI 总开关**：每个 `_init_*` 函数都会读 `ai_config.get_config("enable")`，
> 关闭时直接 `return`。`create_core_tables` 同样会跳过 AI 表创建，
> 因此关闭 AI 时不会创建任何 AI 相关 SQLite 表，节省体积。

### 7.5 关闭钩子（`on_core_shutdown`）

`app_life.lifespan` 在 `yield` 之后会 `shutdown_event.set()` + 调
`core_shutdown_execute()`：

| 钩子函数 | 模块 | 优先级 | 功能 |
|----------|------|--------|------|
| `flush_ai_sessions_on_shutdown` | `ai_core/startup.py` | 0 | `AISessionRegistry.shutdown_all()` 强制把所有未达兜底间隔的 AI 会话日志落盘 |
| `_flush_user_buffer_on_shutdown` | `handler.py` | 0 | 用户写缓冲区最终刷写，防止丢数据 |
| `save_global_val` | `buildin_plugins/.../command_global_val.py` | 0 | 持久化 Bot 流量统计与全局变量 |
| `shutdown_scheduled_tasks` | `ai_core/scheduled_task/startup.py` | 0 | 清理已完成的 APScheduler job |
| `shutdown_meme_module` | `ai_core/meme/startup.py` | 0 | 停止 Meme 打标 worker |
| `shutdown_ai_core_statistics` | `ai_core/statistics/startup.py` | 0 | 持久化统计数据 |
| `_on_shutdown`（MCP 工具） | `ai_core/mcp/startup.py` | 5 | 清理 MCP 客户端 |
| `_on_shutdown`（MCP Server） | `ai_core/mcp/server.py` | 10 | 关闭 MCP Server |
| `_stop_rm_cleanup` | `utils/resource_manager.py` | 10 | 停止 ResourceManager 清理任务 |
| `shutdown_memory_system` | `ai_core/memory/startup.py` | 20 | 停止 IngestionWorker 独立线程 + 多模态摄入 worker |

---

## 八、Web 服务启动

### 8.1 uvicorn 配置

```python
# gsuid_core/core.py

config = uvicorn.Config(
    app,                          # FastAPI app
    host=HOST,                    # 监听地址
    port=PORT,                    # 监听端口
    log_config=None,              # 使用自定义日志
    loop="asyncio",              # asyncio 事件循环
)

server = uvicorn.Server(config)

await server.serve()
```

### 8.2 WebSocket 端点

```python
# gsuid_core/core.py::websocket_endpoint()

@app.websocket("/ws/{bot_id}")
async def websocket_endpoint(websocket: WebSocket, bot_id: str):
    client_host = websocket.client.host

    # 1. IP 访问控制（被 ban / 不在 TRUSTED_IPS 时强制要求 WS_TOKEN）
    if sec_manager.is_banned(client_host):
        await websocket.close(code=1008)
        return

    if not sec_manager.is_trusted(client_host):
        if not WS_SECRET_TOKEN:
            await websocket.close(code=1008)
            return
        token = websocket.query_params.get("token")
        if token != WS_SECRET_TOKEN:
            sec_manager.record_failure(client_host)
            await websocket.close(code=1008)
            return
        sec_manager.record_success(client_host)

    # 2. 建立连接（含 send_worker 启动 / 5 分钟内重连复用旧 _Bot）
    bot = await gss.connect(websocket, bot_id)

    # 3. 两个并发协程：
    #    start():   receive_bytes（带 1s 超时检查 shutdown_event）→ handle_event
    #    process(): bot._process(shutdown_event) 任务消费循环（队列 + sem 并发）
    await asyncio.gather(process(), start())
    # 任一协程退出时，finally 调用 gss.disconnect(bot_id)
```

> **注意**：`gss.connect()` 内部会调用 `bot.start_send_worker()` 启动独立的发送 worker，
> 确保所有 WebSocket 写入通过发送队列串行化执行，避免多任务并发写入导致连接不稳定。
> `receive_bytes` 套了 1 秒超时，是为了让 `shutdown_event` 能尽快从 polling 退出。

### 8.3 HTTP 端点 (可选)

```python
if ENABLE_HTTP:
    _bot = _Bot("HTTP")

    @app.post("/api/send_msg")
    async def sendMsg(msg: Dict):
        MR = msgjson.Decoder(MessageReceive).decode(msgjson.encode(msg))
        result = await handle_event(_bot, MR, True)
        if result:
            return {"status_code": 200, "data": to_builtins(result)}
        else:
            return {"status_code": -100, "data": None}
```

### 8.4 Bot连接管理（含 5 分钟重连复用）

```python
# gsuid_core/server.py::GsServer

class GsServer:
    def __init__(self):
        self.active_ws: Dict[str, WebSocket] = {}    # WebSocket连接
        self.active_bot: Dict[str, _Bot] = {}        # _Bot 实例（断开后保留 5 分钟以便重连复用）

    async def connect(self, websocket: WebSocket, bot_id: str) -> _Bot:
        """建立 Bot 连接。

        - 首次连接：直接 `_Bot(bot_id, websocket)` + `start_send_worker()`。
        - 5 分钟内重连：复用旧实例，仅替换 `bot.bot = websocket` 与 logger，
          原有 `_send_queue` 中尚未发送的消息会继续投递。
        - 超过 5 分钟：cancel 旧 `_send_task` + `clear_send_queue()` 后丢弃旧实例，
          避免内存泄漏。
        """
        ...
        # 触发 GsServer.bot_connect_def 中注册的回调（@on_bot_connect）

    async def disconnect(self, bot_id: str):
        """断开 Bot 连接但**保留实例**用于重连复用。

        清理步骤：
        1. close + 删除 active_ws 中的 WebSocket
        2. cancel 该 bot 的 `_send_task`（避免孤儿协程）
        3. 标记 `bot.bot = None` + `bot._disconnected_at = time.time()`
        4. cancel 全部 bg_tasks 并 await 真正结束
        5. 清理 `Bot.instances` / `Bot.mutiply_instances` / `Bot.mutiply_map`
           中属于此 bot_id 的会话；`active_bot[bot_id]` **不删除**——下次 connect 复用
        """
```

**`_Bot` 发送队列架构**：

```python
# gsuid_core/bot.py::_Bot

class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        self.bot_id = _id
        self.bot = ws
        self.queue = asyncio.queues.PriorityQueue()      # 任务队列
        self._send_queue: asyncio.queues.Queue = ...     # 独立发送队列
        self._send_task: Optional[asyncio.Task] = None   # 发送 worker 任务

    async def _send_worker(self):
        """独立的发送 worker，从发送队列中取出消息并串行发送"""
        while True:
            coro = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
            await coro
            self._send_queue.task_done()

    def start_send_worker(self):
        """启动独立的发送 worker（在 WebSocket 连接时调用）"""
        self._send_task = asyncio.create_task(self._send_worker())

    async def target_send(self, ...):
        """发送消息（通过发送队列串行化）"""
        # ... 消息处理逻辑 ...
        if self.bot:
            body = msgjson.encode(send)
            ws = self.bot
            async def _do_send(ws=ws, body=body):
                await ws.send_bytes(body)
            await self._enqueue_send(_do_send())
```

> **设计目的**：所有 WebSocket 写入操作通过 `_send_queue` 串行化执行，
> 避免 AI 回复、Heartbeat 主动发言、定时任务等多个任务同时写入 WebSocket 导致帧乱序或连接不稳定。

---

## 九、启动检查清单

### 阶段一：同步阻塞（在 `await server.serve()` 之前）

| 步骤 | 操作 | 文件 |
|------|------|------|
| 1 | 切换 HuggingFace 镜像源 (`HF_ENDPOINT` env) | `core.py::main()` |
| 2 | 数据库初始化（建库 + 基础表） | `utils/database/base_models.py::init_database()` |
| 3 | **插件加载**（发现 → 依赖合并安装 → 模块导入） | `server.py::load_plugins()` |
| 4 | 触发器装饰器执行 → `@sv.on_xxx` / `to_ai="…"` 注册到 `_TOOL_REGISTRY["by_trigger"]` | 插件 `__init__.py` |
| 5 | 插件配置批量持久化 + 写回 `config.json` | `config.py::plugin_config_store.save_all()` / `core_config.lazy_write_config()` |
| 6 | `import gsuid_core.ai_core.startup` → 注册 `init_ai_core` | `core.py::main()` |
| 7 | 命令行覆盖 `--port` / `--host` 写入 `core_config` | `core.py::main()` |
| 8 | 构造 FastAPI app（绑定 `lifespan`） | `app_life.py` |
| 9 | 注册 `/ws/{bot_id}` + 可选 `/api/send_msg` | `core.py::websocket_endpoint` |
| 10 | 启动 uvicorn `server.serve()` → 触发 `lifespan` | `core.py::main()` |

### 阶段二：`lifespan` 启动序列

| 步骤 | 操作 | 备注 |
|------|------|------|
| 11 | `await core_start_before_execute()` | 按优先级分组并发：`move_database` → `create_core_tables` → `trans_adapter` → `load_global_val` |
| 12 | `asyncio.create_task(check_speed())` | 后台异步：测速选源 |
| 13 | `asyncio.create_task(core_start_execute())` | **后台异步**：跑 `_start_rm_cleanup` + `init_ai_core` 等全部 `@on_core_start` |
| 14 | `asyncio.create_task(_bgsetup_frontend_b())` | 后台异步：网页控制台前端文件准备 |
| 15 | `await start_scheduler()` | 启动 APScheduler |
| 16 | `asyncio.create_task(clean_log())` | 后台异步：日志清理 |
| 17 | `yield` → 阶段二完成，uvicorn 开始接受连接 | 此时 WebSocket 已可连接，AI 子系统仍在后台逐步上线 |

### 阶段三：关闭流程（`yield` 之后）

| 步骤 | 操作 |
|------|------|
| 18 | 收到 SIGINT/SIGTERM → `shutdown_event.set()`（Windows 仅靠 uvicorn 信号） |
| 19 | `shutdown_scheduler()` |
| 20 | `core_shutdown_execute()` 按优先级分组并发跑所有 `@on_core_shutdown` |

---

## 十、开发模式

### 10.1 启动参数

```bash
# 启动开发模式 (只加载 -dev 后缀插件)
python -m gsuid_core --dev

# 指定端口
python -m gsuid_core --port 8888

# 指定地址 (0.0.0.0 = 监听全部地址)
python -m gsuid_core --host 0.0.0.0

# 组合使用
python -m gsuid_core --dev --port 8888 --host 0.0.0.0
```

### 10.2 开发模式区别

```python
# server.py::load_plugins()
if dev_mode:
    # 只加载 name.endswith("-dev") 的插件
    if not plugin.name.endswith("-dev"):
        continue
```

### 10.3 开发模式插件命名

```
# 普通插件（开发模式不加载）
gsuid_core/plugins/my_plugin/__init__.py

# 开发模式插件
gsuid_core/plugins/my_plugin-dev/__init__.py
```

---

## 十一、启动失败排查

### 11.1 常见错误

| 错误 | 可能原因 | 解决方案 |
|------|----------|----------|
| `ModuleNotFoundError` | 依赖未安装 | 检查pyproject.toml或手动pip install |
| `Port already in use` | 端口被占用 | 更换端口或关闭占用进程 |
| `WS_TOKEN` 警告 | 未配置WebSocket令牌 | 配置WS_TOKEN或仅本地访问 |
| 数据库连接失败 | 数据库文件权限问题 | 检查数据目录权限 |

### 11.2 日志查看

```bash
# 查看实时日志
tail -f logs/gsuid_core.log

# 查看ERROR级别日志
grep ERROR logs/gsuid_core.log
```

### 11.3 健康检查

```bash
# 检查Web服务是否正常
curl http://localhost:8765/api/system/info

# 检查WebSocket连接
ws://localhost:8765/ws/test_bot?token=<WS_TOKEN>
```

---

## 十二、Bot 与 _Bot 类详解

### 12.1 类层次结构

```
_Bot (底层实现)
  │
  │  包装
  ▼
Bot (高层包装器)
  │
  │  代理
  ▼
MockBot (AI 调用时的代理)
```

### 12.2 `_Bot` 类 — 底层 Bot 实现

**文件**: `gsuid_core/bot.py`

`_Bot` 是底层的 Bot 实现，负责管理 WebSocket 连接、消息队列和发送调度。

```python
class _Bot:
    def __init__(self, _id: str, ws: Optional[WebSocket] = None):
        self.bot_id = _id          # Bot 标识符
        self.bot = ws              # WebSocket 连接（可为 None，如 HTTP 模式）
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.PriorityQueue()  # 任务队列
        self.send_dict = {}        # HTTP 模式下的发送字典
        self.bg_tasks = set()      # 后台任务集合
        self.sem = asyncio.Semaphore(10)  # 并发控制
        self._send_queue = asyncio.queues.Queue()  # 独立发送队列
        self._send_task = None     # 发送 worker 任务
```

**核心职责**:
- 管理 WebSocket 连接的生命周期
- 通过 `_send_queue` 串行化 WebSocket 发送，避免并发写入
- 通过 `queue` + `sem` 管理任务执行的并发度
- 提供 `target_send()` 方法处理消息格式转换、历史记录、记忆系统集成
- 提供 `_process()` 方法作为任务消费循环

**关键方法**:
| 方法 | 说明 |
|------|------|
| `target_send()` | 底层发送方法，处理消息转换、Markdown、按钮、历史记录 |
| `_send_worker()` | 独立发送 worker，从发送队列串行执行 |
| `_process()` | 任务消费循环，支持 shutdown_event 优雅关闭 |
| `wait_task()` | HTTP 模式下等待任务完成并返回结果 |

### 12.3 `Bot` 类 — 高层包装器

**文件**: `gsuid_core/bot.py`

`Bot` 是供插件和触发器使用的高层包装器，包装 `_Bot` + `Event`，提供面向业务的 API。

```python
class Bot:
    instances: Dict[str, "Bot"] = {}           # 单轮交互实例
    mutiply_instances: Dict[str, "Bot"] = {}   # 多轮交互实例
    mutiply_map: Dict[str, str] = {}           # 多轮交互映射

    def __init__(self, bot: _Bot, ev: Event):
        self.bot = bot              # 底层 _Bot 实例
        self.ev = ev                # 当前事件
        self.bot_id = ev.bot_id
        self.bot_self_id = ev.bot_self_id
        self.session_id = f"{self.bid}%%%{self.temp_gid}%%%{self.uid}"
```

**核心职责**:
- 封装 `_Bot` + `Event` 的组合，提供简洁的 `send()` API
- 管理交互式会话（单轮/多轮等待用户回复）
- 处理按钮、Markdown 模板等平台适配逻辑

**关键方法**:
| 方法 | 说明 |
|------|------|
| `send()` | 发送消息，自动从 `ev` 提取目标信息 |
| `receive_resp()` | 发送消息并等待用户回复（交互式） |
| `send_option()` | 发送带选项按钮的消息 |
| `wait_for_key()` | 等待用户回复 |
| `target_send()` | 指定目标发送消息 |

### 12.4 `MockBot` 类 — AI 调用代理

**文件**: `gsuid_core/ai_core/trigger_bridge.py`

`MockBot` 是 AI 调用触发器时使用的代理 Bot，拦截 `send()` 将内容收集而非真正发送。

```python
class MockBot:
    def __init__(self, real_bot: Bot, ctx: Dict[str, Any]):
        self._real_bot = real_bot   # 真实 Bot 实例
        self._ctx = ctx             # 收集上下文

    async def send(self, message, at_sender=False):
        # 文本 → 存入 ctx["bot_messages"]
        # 图片 → RM.register() → 存入 ctx["image_ids"]

    def __getattr__(self, name):
        # 其他属性代理到 real_bot
```

### 12.5 使用场景对照

| 场景 | 使用的类 | 说明 |
|------|----------|------|
| 框架启动、WebSocket 连接 | `_Bot` | 底层连接管理 |
| 插件触发器函数参数 `bot: Bot` | `Bot` | 高层 API，插件直接使用 |
| AI Agent 调用触发器 | `MockBot` 包装 `Bot` | 拦截发送，收集返回值 |
| MCP Server 调用触发器 | `MockBot` 包装 `Bot` | 同 AI Agent，但无 AI 上下文 |
| HTTP API 模式 | `_Bot("HTTP")` | 无 WebSocket，通过 send_dict 返回 |

### 12.6 关键区别总结

| 特性 | `_Bot` | `Bot` |
|------|--------|-------|
| 构造参数 | `_id: str, ws: Optional[WebSocket]` | `bot: _Bot, ev: Event` |
| 依赖 Event | ❌ 不依赖 | ✅ 强依赖 |
| send 方法 | `target_send()` 需要完整参数 | `send()` 自动从 ev 提取 |
| 交互式等待 | ❌ 不支持 | ✅ `receive_resp()` |
| 按钮/模板 | ❌ 不处理 | ✅ 平台适配 |
| 实例管理 | 无 | `instances` / `mutiply_instances` |
| 适用场景 | 框架内部、连接管理 | 插件开发、触发器函数 |

> **⚠️ 重要**: 在需要 `Bot` 类型的场景中（如 `MockBot.__init__`、触发器函数参数），**必须**传入 `Bot` 实例而非 `_Bot` 实例。`Bot` 包装了 `_Bot` + `Event`，缺少任何一个都会导致运行时错误。
