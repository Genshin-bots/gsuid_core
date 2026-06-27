# 三、插件加载与配置系统

> **返回主入口**：[`../SKILL.md`](../SKILL.md) · **上一章**：[二、启动时序与生命周期](./02-startup-lifecycle.md) · **下一章**：[四、事件处理与触发器流转](./04-event-trigger-flow.md)

本章讲插件怎么被发现/装依赖/import，以及配置系统（核心配置 + 插件配置 + SV 配置）怎么
组织、怎么热重载。改插件加载逻辑、动配置结构、加配置热重载前必读。

## 3.1 插件加载总流程（`server.py::load_plugins`）

```python
async def load_plugins(self, dev_mode=False):
    refresh_installed_dependencies()
    # 把仓库根目录加入 sys.path，让插件能 from gsuid_core ...
    # 阶段一：发现插件 + 收集缺失依赖（不立即 pip）
    # 阶段二：flush_pending_installs() —— 合并所有插件缺失依赖，一次 pip 调用（含镜像源 fallback）
    # 阶段三：依次 cached_import 模块；模块级 @sv.on_xxx / @ai_tools / ai_entity 在此触发
    # 阶段四：plugin_config_store.save_all() + core_config.lazy_write_config()
```

> **关键设计**：依赖**先收集后合并安装**。早期"每个插件单独跑 pip + 镜像源 fallback"导致
> 首次启动巨慢，现在合并成一次 pip 调用。
>
> `core_start_def` 集合**不在** `load_plugins()` 内触发，而是由 `app_life.lifespan` 在 WS
> 启动后 `asyncio.create_task(core_start_execute())` 跑（见 [§02](./02-startup-lifecycle.md)）。

## 3.2 插件发现与分类

```
gsuid_core/
├── plugins/                # 用户插件目录
│   ├── plugin_a/__init__.py        # 单插件包
│   ├── plugin_b/__full__.py        # 全量加载（加载文件夹内所有 .py）
│   ├── plugin_c/__nest__.py        # 嵌套加载
│   └── single_plugin.py            # 单文件插件
└── buildin_plugins/        # 内置插件目录
```

| 文件存在 | 加载方式 | 说明 |
|----------|----------|------|
| `__init__.py` | 单插件包 | 文件夹作为单个插件包 |
| `__full__.py` | 全量加载 | 加载文件夹内所有 `.py` 模块 |
| `__nest__.py` | 嵌套加载 | 嵌套模式 |
| `*.py`（单文件） | 单文件插件 | 直接作为插件导入 |

判定逻辑在 `load_plugin()`：目录下优先 `__full__.py`（全量）→ `__nest__.py`/`plugin/plugin.py`
（嵌套）→ `__init__.py`（单包）；非目录则单文件。

## 3.3 依赖管理

`check_pyproject()` 支持两种格式：PEP 621（`[project]` 表）与 Poetry（`[tool.poetry]` 表）。

依赖安装流程：`normalize_name()` 规范化（小写、`-_.` 互换）→ 在 `ignore_dep` 列表则跳过 →
未安装则入队 → 已装但版本不符则检查更新 → `install_packages()` 依次试镜像源。

```python
mirrors = [字节(Volces), 阿里(Aliyun), 清华(Tsinghua), 官方(PyPI)]
ignore_dep = {"python", "fastapi", "pydantic", "gsuid-core", "toml", "packaging"}
```

> 基础依赖（`ignore_dep`）永不被插件触发安装，避免插件声明的版本约束把框架核心包降级。

## 3.4 模块导入与循环导入（`cached_import`）

```python
def cached_import(self, module_name, filepath, _type):
    if module_name in _module_cache: return _module_cache[module_name]
    spec = importlib.util.spec_from_file_location(module_name, filepath)
    module = importlib.module_from_spec(spec)
    sys.modules[module_name] = module      # 先放入 sys.modules（处理循环导入）
    try:
        spec.loader.exec_module(module)    # 执行模块 → 触发 @sv.on_command 等装饰器
    except Exception:
        del sys.modules[module_name]       # 失败清理 dirty module
        raise
    _module_cache[module_name] = module
```

> **要点**：模块**先注册进 `sys.modules` 再执行**，这样 A import B、B 回头 import A 时拿到的是
> placeholder 而非重新加载，避免循环导入死锁。import 失败会清掉半加载模块，防止脏状态。
> 触发器的注册副作用（`@sv.on_xxx` / `@ai_tools`）正是在 `exec_module` 这一刻发生的。

## 3.5 配置系统总览

GsCore 有**三层配置**，物理上分开存储：

| 层 | 类 | 存储 | 内容 |
|----|----|----|----|
| 核心配置 | `CoreConfig` | `data/config.json` | HOST/PORT/masters/WS_TOKEN/log… |
| 插件配置 | `PluginConfigStore` | `data/plugins_configs/<plugin>.json` | 每个插件一份独立 JSON |
| SV 配置 | `SV` | 嵌在插件配置的 `sv` 键 | 服务模块级开关/权限 |
| AI 配置 | `ai_config`（`StringConfig`） | `data/ai_core/...json` | AI 总开关/模型/记忆/嵌入… |

### 3.5.1 核心配置 `CoreConfig`

```python
CONFIG_DEFAULT = {
    "HOST": "localhost", "PORT": "8765", "ENABLE_HTTP": False,
    "WS_TOKEN": "", "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"],
    "masters": [], "superusers": [], "REGISTER_CODE": _generate_register_code(),
    "misfire_grace_time": 90, "log": {...},
    "enable_empty_start": True, "command_start": [],
    "buffered_user_writes": False, "sv": {},
}
```

加载流程：`CoreConfig.__init__` → 若存在旧 `gsuid_core/config.json` 则 `shutil.copy2` 迁到
`data/config.json` 并删源（一次性迁移）→ 不存在则写默认 → `update_config()` 读 + **合并默认
（填补缺失项）**。

> **Breaking Change（历史）**：`plugins` key 已从 `config.json` 移除，每个插件配置改为独立
> `data/plugins_configs/<plugin>.json`。改核心配置结构时，新增项放进 `CONFIG_DEFAULT` 即可
> （`update_config` 会自动给老用户补上）。

### 3.5.2 插件配置 `PluginConfigStore`

```python
class PluginConfigStore:
    def __init__(self):
        self._migrate_from_config()  # 启动时把旧 config.json["plugins"] 拆成独立 JSON 并移除该 key
        self._load_all()             # 加载 data/plugins_configs/*.json 到内存缓存
    def get_all(self) -> Dict[str, dict]: ...   # 与旧 config_plugins 兼容
    def save(self, plugin_name): ...            # 持久化单个插件配置
    def save_all(self): ...                      # 持久化所有
```

迁移时会先备份 `config.json → data/config_backup.json`（若不存在）再拆分。

### 3.5.3 SV 配置注册（`sv.py`）

```python
class Plugins:   # 单例：name 已存在则复用，否则从 plugins_sample deepcopy 一份并 save
class SV:
    def __init__(self, name="", ...):
        plugin_config = config_plugins[self.self_plugin_name]   # 从 PluginConfigStore 缓存取
        self.sv = plugin_config.get("sv", {}).get(name, {})     # SV 级配置嵌在插件配置的 sv 键下
```

`SV` 自动从调用栈推断归属的 `Plugins`。SV 级别的 `enabled` / `pm`（权限等级）用于触发器
匹配与 `to_ai` 工具的权限检查（见 [§04](./04-event-trigger-flow.md) 与 [§07](./07-tool-registry-and-agent.md)）。

## 3.6 配置热重载

`StringConfig.set_config` 改内存后**立即持久化**：

```python
def set_config(self, key, value):
    if key in self.config_list:
        self.config[key].data = value   # 1. 改内存
        self.write_config()             # 2. 立即落盘 json.dump
        return True
    return False
```

**热重载矩阵**（AI / Persona 相关）：

| 配置项 | 热重载 | 生效时机 |
|--------|--------|----------|
| `enable`（AI 总开关） | ✅ | 下次消息处理（`handle_ai` 函数内动态读取） |
| `ai_black_list` / `ai_white_list` | ✅ | 下次消息处理 |
| `scope` / `target_groups` | ✅ | 下次会话匹配 |
| `ai_mode` | ✅ | 下次消息处理 |
| `keywords` | ✅ | 下次消息处理 |
| `inspect_interval` | ⚠️ 需重启巡检 | API 里自动 `stop_for_persona` + `start_for_persona` |
| `high_level_provider_config_name` / `low_level_provider_config_name`（切换高/低级任务模型） | ✅ | **存活会话下次 run 即时热替换**：`GsCoreAIAgent.refresh_model_if_changed()` 在 `run()` 内用「全名 + 内容指纹」双键比对（`model_config_name` + `model_config_fingerprint`），任一变了就就地换 `self.model`（**保留对话历史**，并关闭旧模型 HTTP 客户端释放连接池），无需 `coreclear` |
| 同一配置文件内改字段（`model_name` / `base_url` / `model_effort` / `request_method`） | ✅ | 全名不变但内容指纹变（`get_model_fingerprint_for_task()` 对激活配置 dict 取 sha256），存活会话下次 run 同样热替换；新建 Session 则每次 `get_model_for_task()` 动态读最新 |
| `request_method`（OpenAI 请求方式：`chat_completions` ↔ `responses`） | ✅ | 仅 OpenAI provider；`get_openai_model_by_name()` 据此构造 `OpenAIChatModel`(/v1/chat/completions) 或 `OpenAIResponsesModel`(/v1/responses)，对 Agent 接口一致，TTFT/TPS/工具调用/日志全部复用 |
| Persona `system_prompt` | ✅ | 改 persona 文件后 mtime 检测自动重载（见 [§06](./06-ai-session-and-persona.md)） |

> **写新配置项时的约定**：默认值放进对应 `setup_config()` / `CONFIG_DEFAULT`；只要消费侧
> 是"每次用时读"而非"启动时缓存进模块变量"，就自动获得热重载。`inspect_interval` 例外是
> 因为它绑定了一个 APScheduler job，必须显式重建。
