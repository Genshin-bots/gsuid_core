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
       │                          │                        │                加载 __init__.py
       │                          │                        │                     │
       │                          │                        │                解析 pyproject.toml
       │                          │                        │                     │
       │                          │                        │                自动安装依赖
       │                          │                        │                     │
       │                          │                        │                cached_import()
       │                          │                        │                     │
       │                          │                        │              注册 @sv.on_xxx
       │                          │                        │                     │
       │                          │                        │◄────────────────────│
       │                          │                        │                     │
       │                          │                        │                  加载完成
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
       ▼                          ▼                        ▼                     ▼
```

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
    # 1. 刷新已安装依赖
    refresh_installed_dependencies()

    # 2. 添加根目录到 sys.path
    root_path = str(Path(__file__).parents[1])
    if root_path not in sys.path:
        sys.path.append(root_path)

    # 3. 遍历插件目录
    plug_path_list = [
        p for p in list(BUILDIN_PLUGIN_PATH.iterdir()) + list(PLUGIN_PATH.iterdir())
        if p.is_dir() or (p.is_file() and p.suffix == ".py")
    ]

    # 4. 加载每个插件
    for plugin in plug_path_list:
        if dev_mode and not plugin.name.endswith("-dev"):
            continue
        d = self.load_plugin(plugin, dev_mode)
        if isinstance(d, str):  # 加载失败
            continue
        all_plugins.extend(d)

    # 5. 导入所有模块
    for module_name, filepath, _type in all_plugins:
        try:
            self.cached_import(module_name, filepath, _type)
        except Exception as e:
            logger.exception(f"❌ 插件{filepath.stem}导入失败")

    # 6. 写入配置
    core_config.lazy_write_config()
```

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
    "REGISTER_CODE": secrets.token_hex(16),
    "misfire_grace_time": 90,
    "log": {...},
    "enable_empty_start": True,
    "command_start": [],
    "sv": {},      # SV 配置
    "plugins": {},  # 插件配置
}
```

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

### 6.3 插件配置注册

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
```

### 6.4 SV 配置注册

```python
# gsuid_core/sv.py::SV

class SV:
    def __init__(self, name: str = "", ...):
        # 从 config_plugins 获取插件配置
        plugin_config = config_plugins[self.self_plugin_name]

        # 设置 SV 级别的配置
        self.sv = plugin_config.get("sv", {}).get(name, {})
```

---

## 七、Web 服务启动

### 7.1 uvicorn 配置

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

### 7.2 WebSocket 端点

```python
# gsuid_core/core.py::websocket_endpoint()

@app.websocket("/ws/{bot_id}")
async def websocket_endpoint(websocket: WebSocket, bot_id: str):
    # 1. IP 访问控制
    client_host = websocket.client.host
    if sec_manager.is_banned(client_host):
        await websocket.close(code=1008)
        return

    # 2. Token 验证 (如果配置了 WS_TOKEN)
    token = websocket.query_params.get("token")
    if not sec_manager.is_trusted(client_host):
        if not WS_SECRET_TOKEN:
            await websocket.close(code=1008)
            return
        if token != WS_SECRET_TOKEN:
            sec_manager.record_failure(client_host)
            await websocket.close(code=1008)
            return

    # 3. 建立连接
    bot = await gss.connect(websocket, bot_id)

    # 4. 启动读写并发
    await asyncio.gather(process(), start())
```

### 7.3 HTTP 端点 (可选)

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

---

## 八、启动检查清单

| 步骤 | 操作 | 文件 |
|------|------|------|
| 1 | 数据库初始化 | `utils/database/base_models.py::init_database()` |
| 2 | 插件加载 | `server.py::load_plugins()` |
| 3 | 依赖安装 | `server.py::check_pyproject()` → `process_dependencies()` |
| 4 | 模块导入 | `server.py::cached_import()` |
| 5 | 配置合并 | `config.py::CoreConfig.update_config()` |
| 6 | WebSocket 服务 | `core.py::websocket_endpoint()` |
| 7 | HTTP 服务 (可选) | `core.py::sendMsg()` |

---

## 九、开发模式

```bash
# 启动开发模式 (只加载 -dev 后缀插件)
python -m gsuid_core --dev

# 指定端口
python -m gsuid_core --port 8888

# 指定地址
python -m gsuid_core --host 0.0.0.0
```

```python
# 开发模式区别
if dev_mode:
    # 只加载 name.endswith("-dev") 的插件
    if not plugin.name.endswith("-dev"):
        continue
```
