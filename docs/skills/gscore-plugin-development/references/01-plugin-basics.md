# 一、插件基础结构

## 1.1 推荐目录结构（嵌套加载模式）

参照 **ZZZeroUID** / **SayuStock** 等成熟插件，**推荐使用嵌套加载模式**——外层是
"插件包"（含 `pyproject.toml` / `README.md` / `LICENSE` / `ICON.png` / `__nest__.py`），
内层是与插件同名的 Python 包，所有业务子模块写在里面：

```
gsuid_core/plugins/MyPlugin/                 ← 插件根目录（仓库名）
├── __init__.py                              ← 外层入口（一般留空）
├── __nest__.py                              ← 空文件，标记"嵌套加载"
├── pyproject.toml                           ← 插件元数据 + 依赖
├── README.md
├── LICENSE
├── ICON.png                                 ← 插件图标（帮助 / webconsole 展示）
└── MyPlugin/                                ← 内层包（与插件目录同名）
    ├── __init__.py                          ← 定义 Plugins(...)，import 子模块触发注册
    ├── __full__.py                          ← 空文件，向框架标记"扫描子目录全部导入"
    ├── version.py                           ← MyPlugin_version = "1.0.0"
    ├── myplugin_bind/                       ← 业务子模块（前缀 = 插件名小写缩写）
    │   └── __init__.py
    ├── myplugin_roleinfo/                   ← 业务子模块
    │   ├── __init__.py
    │   ├── draw_roleinfo.py                 ← 图片渲染层（PIL / htmlkit）
    │   └── texture2d/                       ← 子模块专属静态资源
    ├── myplugin_help/                       ← 帮助子模块
    │   ├── __init__.py
    │   ├── get_help.py
    │   ├── help.json                        ← 帮助数据
    │   ├── icon_path/                       ← 各分组图标
    │   └── texture2d/                       ← 帮助底图
    ├── myplugin_config/                     ← 配置子模块
    │   ├── __init__.py                      ← 命令：开启/关闭/设置阈值 等
    │   ├── config_default.py                ← CONFIG_DEFAULT: Dict[str, GSC]
    │   └── myplugin_config.py               ← MYPLUGIN_CONFIG = StringConfig(...)
    └── utils/                               ← 共享工具（**不加前缀**）
        ├── database/
        │   ├── __init__.py
        │   └── models.py                    ← 数据模型 + @site.register_admin
        ├── resource/
        │   └── RESOURCE_PATH.py             ← 资源 / 配置 / 数据路径常量
        ├── image.py                         ← get_footer / get_ICON / 颜色相关
        ├── api.py                           ← 第三方 API 客户端
        └── hint.py                          ← 公共提示文案
```

**命名规范**（参照 GenshinUID / ZZZeroUID / SayuStock）：

| 规则 | 示例 |
|------|------|
| 插件目录名 | `MyPlugin/`（用户插件）；buildin 插件用 `_PluginName/` 前缀下划线 |
| 内部包名 | 与插件目录名同名（不含下划线前缀） |
| 入口文件 | 外层 `__init__.py` + 外层 `__nest__.py` + 内层 `__init__.py` + 内层 `__full__.py` |
| 子模块目录 | `{prefix}_{feature}/`，`prefix` 为插件名小写 |
| 共享工具 | `utils/`、`tools/`（不加前缀） |
| 配置模块 | `{prefix}_config/` |
| 静态资源 | 子模块内的 `texture2d/`（仅本模块用）或外层 `ICON.png`（插件级） |

## 1.2 入口三件套

**外层 `MyPlugin/__init__.py`**（可留空，仅作 Python 包标记）：
```python
"""init"""
```

**外层 `MyPlugin/__nest__.py`**（空文件，向框架声明启用嵌套加载）：
```python
```

> 关键作用：框架在加载插件目录时若发现 `__nest__.py`，会改为遍历内层包下所有子模块自动 import，
> 不需要在内层 `__init__.py` 手动一一 `from . import xxx`。配合 `__full__.py` 让 import 顺序稳定。

**内层 `MyPlugin/MyPlugin/__init__.py`**（声明 `Plugins`，触发子模块注册）：
```python
"""init"""
from gsuid_core.sv import Plugins

# Plugins 是单例：定义这一整个"插件"的前缀、权限、别名
# 同名 plugin_name 在框架内只创建一次，所有 SV 自动归属到它。
Plugins(
    name="MyPlugin",
    force_prefix=["mp", "我的插件"],   # 强制前缀，用户必须以此开头
    allow_empty_prefix=False,         # 不允许无前缀触发
    alias=["my_plugin"],              # 插件别名
)

# 若用了 __full__.py 嵌套加载，则不需要手动 import 子模块；
# 否则要在这里依次 from . import xxx，触发子模块的 @sv.on_xxx 注册。
```

**内层 `MyPlugin/MyPlugin/__full__.py`**（空文件）：
```python
```

## 1.3 `Plugins` vs `SV` 的层级关系

| | `Plugins` | `SV` |
|---|---|---|
| 层级 | **插件级**（整个项目） | **服务模块级**（一组功能） |
| 数量 | 一个插件目录一个 | 一个插件下可有多个（按业务分组） |
| 配置项 | `name` / `force_prefix` / `prefix` / `allow_empty_prefix` / `disable_force_prefix` / `alias` / `pm` / `area` | `name` / `pm` / `priority` / `enabled` / `area` / `black_list` / `white_list` |
| 持久化 | `plugin_config_store`（webconsole 可改） | `config_sv` 中（webconsole 可改） |
| 文件位置 | 内层 `__init__.py` 顶部 | 各子模块 `__init__.py` 顶部 |
| 创建语义 | 单例，重复声明返回同一实例 | 同 `name` 的 SV 是单例（跨文件共享） |

`SV("xxx")` 在内部会自动从调用栈推断"我属于哪个 plugin"（按 `plugins/` 或 `buildin_plugins/`
下一级目录名），不需要显式声明归属。

## 1.4 pyproject.toml（声明插件依赖）

```toml
[project]
name = "my-plugin"
version = "0.1.0"
dependencies = [
    "httpx>=0.24.0",
    "pillow>=9.0.0",
    "aiofiles>=23.0.0",
    # 仅当需要 HTML 渲染时（详见第九章）
    # "pyrenderhtml>=0.0.5",
    # 仅当 PIL / htmlkit 都不够时
    # "playwright>=1.49.0",
]
```

启动时自动安装 `dependencies` 中声明的依赖。`python`、`fastapi`、`pydantic`、`gsuid-core`、
`sqlmodel`、`apscheduler`、`pydantic-ai` 等框架基础依赖**无需重复声明**。

## 1.5 资源路径约定（推荐 `utils/resource/RESOURCE_PATH.py`）

把所有运行时数据 / 配置 / 缓存 / 用户数据的绝对路径集中在一个文件，业务代码统一引用，
避免到处散落 `get_res_path()`：

```python
# MyPlugin/MyPlugin/utils/resource/RESOURCE_PATH.py
import sys
from pathlib import Path
from gsuid_core.data_store import get_res_path

# 插件运行时根目录：data/MyPlugin/
MAIN_PATH = get_res_path() / "MyPlugin"
sys.path.append(str(MAIN_PATH))

# 各类持久化路径
CONFIG_PATH = MAIN_PATH / "config.json"        # 配置（StringConfig 用）
PLAYER_PATH = MAIN_PATH / "players"            # 用户数据
RESOURCE_PATH = MAIN_PATH / "resource"         # 远程下载的素材
CACHE_PATH = MAIN_PATH / "cache"               # 缓存
CU_BG_PATH = MAIN_PATH / "bg"                  # 自定义背景

for p in (PLAYER_PATH, RESOURCE_PATH, CACHE_PATH, CU_BG_PATH):
    p.mkdir(parents=True, exist_ok=True)
```
