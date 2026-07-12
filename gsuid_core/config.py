import json
import time
import shutil
import secrets
from typing import Any, Set, Dict, List, Tuple, Union, Literal, Callable, Optional, Sequence, overload
from pathlib import Path
from dataclasses import dataclass

from boltons.fileutils import atomic_save

from gsuid_core.data_store import PLUGINS_CONFIGS_PATH, get_res_path

CONFIG_PATH = get_res_path() / "config.json"
OLD_CONFIG_PATH = Path(__file__).parent / "config.json"


# 生成随机注册码
def _generate_register_code() -> str:
    return secrets.token_hex(16)


# 一个可选项：直接写值（标签=值），或 (值, 展示标签) 二元组（如 ("zh-cn", "简体中文")）。
Choice = Union[str, Tuple[str, str]]


@dataclass(frozen=True)
class SelectOption:
    """「只能从固定集合中选值」的核心配置项（前端渲染为下拉/候选多选）。

    直接写进下面的 CORE_CONFIG 即「默认值 + 可选项一处声明」，前端零改动自动渲染：
    - 静态可选值写 choices，动态可选值（如随已加载语言变化）写 provider，两者形状一致：
      每项是值本身或 (值, 展示标签)。
    - multi=True 表示值为列表、可从候选中多选（如 log.output）。
    """

    default: Any
    choices: Sequence[Choice] = ()
    provider: Optional[Callable[[], Sequence[Choice]]] = None
    multi: bool = False

    def resolve(self) -> Dict[str, Any]:
        """归一化为 WebConsole 下发的元数据：{type, options, labels}。"""
        pairs = [(c, c) if isinstance(c, str) else c for c in (self.provider() if self.provider else self.choices)]
        return {
            "type": "multiselect" if self.multi else "strictselect",
            "options": [value for value, _ in pairs],
            "labels": {value: label for value, label in pairs},
        }


def _language_choices() -> List[Tuple[str, str]]:
    # 运行时局部导入 i18n，避免 config <-> i18n 模块级循环依赖
    from gsuid_core.i18n import lang_display_name, available_language_codes

    return [(code, lang_display_name(code)) for code in available_language_codes()]


# 核心配置单一声明源：普通项直接写默认值；「只能选」的项写 SelectOption（默认值+可选项一处写），
# 嵌套字典内同样生效（如 log.level）。
CORE_CONFIG: Dict[str, Any] = {
    "HOST": "localhost",
    "PORT": "8765",
    # 部署者侧框架文案语言（i18n 回落基准）：默认值 + 可选项就在这一处
    "LANGUAGE": SelectOption("zh-cn", provider=_language_choices),
    "ENABLE_HTTP": False,
    "WS_TOKEN": "",
    "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"],
    "masters": [],
    "superusers": [],
    "REGISTER_CODE": _generate_register_code(),
    "misfire_grace_time": 90,
    "log": {
        "level": SelectOption("INFO", ["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
        "output": SelectOption(["stdout", "stderr", "file"], ["stdout", "stderr", "file"], multi=True),
        "module": False,
    },
    "enable_empty_start": True,
    "command_start": [],
    "buffered_user_writes": False,
    "sv": {},
}


def _unwrap_defaults(node: Any) -> Any:
    """把声明树中的 SelectOption 展开为其默认值，得到纯默认值结构。"""
    if isinstance(node, SelectOption):
        return node.default
    if isinstance(node, dict):
        return {k: _unwrap_defaults(v) for k, v in node.items()}
    return node


def _collect_selects(node: Dict[str, Any], prefix: str = "") -> Dict[str, SelectOption]:
    """收集声明树中全部 SelectOption。

    嵌套 key 用 "_" 扁平化（log.level -> "log_level"），与 WebConsole 前端的字段命名对齐。
    """
    found: Dict[str, SelectOption] = {}
    for k, v in node.items():
        key = f"{prefix}{k}"
        if isinstance(v, SelectOption):
            found[key] = v
        elif isinstance(v, dict):
            found.update(_collect_selects(v, f"{key}_"))
    return found


# 派生：纯默认值字典（现有全部消费者照旧读它）+ 下拉可选项表（供 WebConsole 下发）。
CONFIG_DEFAULT: Dict[str, Any] = _unwrap_defaults(CORE_CONFIG)
CONFIG_OPTIONS: Dict[str, SelectOption] = _collect_selects(CORE_CONFIG)

STR_CONFIG = Literal["HOST", "PORT", "WS_TOKEN", "REGISTER_CODE", "LANGUAGE"]
INT_CONFIG = Literal["misfire_grace_time"]
LIST_CONFIG = Literal["superusers", "masters", "command_start", "TRUSTED_IPS"]
DICT_CONFIG = Literal["sv", "log"]
BOOL_CONFIG = Literal["enable_empty_start", "ENABLE_HTTP", "buffered_user_writes"]


plugins_sample = {
    "name": "",
    "pm": 6,
    "priority": 5,
    "enabled": True,
    "area": "SV",
    "black_list": [],
    "white_list": [],
    "prefix": [],
    "force_prefix": [],
    "disable_force_prefix": False,
    "allow_empty_prefix": False,
    "sv": {},
}


class CoreConfig:
    def __init__(self) -> None:
        self.lock = False
        if OLD_CONFIG_PATH.exists():
            if not CONFIG_PATH.exists():
                shutil.copy2(OLD_CONFIG_PATH, CONFIG_PATH)
            OLD_CONFIG_PATH.unlink()

        if not CONFIG_PATH.exists():
            with open(CONFIG_PATH, "w", encoding="UTF-8") as file:
                json.dump(CONFIG_DEFAULT, file, indent=4, ensure_ascii=False)

        self.update_config()

    def write_config(self):
        import time

        max_retries = 3
        retry_delay = 0.5

        for attempt in range(max_retries):
            try:
                with atomic_save(
                    str(CONFIG_PATH),
                    text_mode=False,
                    overwrite=True,
                    file_perms=0o644,
                ) as file:
                    if file:
                        json_str = json.dumps(
                            self.config,
                            indent=4,
                            ensure_ascii=False,
                        )
                        file.write(json_str.encode("utf-8"))
                    else:
                        raise RuntimeError("写入配置文件失败!")
                return  # 成功写入，直接返回
            except OSError as e:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)  # 等待一段时间后重试
                else:
                    raise RuntimeError(f"写入配置文件失败，已重试 {max_retries} 次: {str(e)}")

    def update_config(self):
        # 打开config.json
        try:
            with open(CONFIG_PATH, "r", encoding="UTF-8") as f:
                self.config: Dict[str, Any] = json.load(f)
        except UnicodeDecodeError:
            with open(CONFIG_PATH, "r") as f:
                self.config = json.load(f)

        # 对没有的值，添加默认值
        for key in CONFIG_DEFAULT:
            if key not in self.config:
                self.config[key] = CONFIG_DEFAULT[key]
            if isinstance(CONFIG_DEFAULT[key], Dict):
                for sub_key in CONFIG_DEFAULT[key]:
                    if sub_key not in self.config[key]:
                        self.config[key][sub_key] = CONFIG_DEFAULT[key][sub_key]

        # 重新写回（必须懒加载）
        # self.write_config()

    @overload
    def get_config(self, key: STR_CONFIG) -> str: ...

    @overload
    def get_config(self, key: DICT_CONFIG) -> Dict: ...

    @overload
    def get_config(self, key: LIST_CONFIG) -> List: ...

    @overload
    def get_config(self, key: INT_CONFIG) -> int: ...

    @overload
    def get_config(self, key: BOOL_CONFIG) -> bool: ...

    def get_config(self, key: str) -> Union[str, Dict, List, int, bool]:
        if key in self.config:
            return self.config[key]
        elif key in CONFIG_DEFAULT:
            self.update_config()
            return self.config[key]
        else:
            return {}

    @overload
    def set_config(self, key: STR_CONFIG, value: str) -> bool: ...

    @overload
    def set_config(self, key: LIST_CONFIG, value: List) -> bool: ...

    @overload
    def set_config(self, key: DICT_CONFIG, value: Dict) -> bool: ...

    @overload
    def set_config(self, key: INT_CONFIG, value: int) -> bool: ...

    @overload
    def set_config(self, key: BOOL_CONFIG, value: bool) -> bool: ...

    def set_config(self, key: str, value: Union[str, List, Dict, int, bool]) -> bool:
        if key in CONFIG_DEFAULT:
            # 设置值
            self.config[key] = value
            # 重新写回
            self.write_config()
            return True
        else:
            return False

    def lazy_write_config(self):
        self.write_config()
        # 同时刷新标记为脏的插件配置
        if "plugin_config_store" in globals():
            plugin_config_store.save_dirty()

    def lazy_set_config(self, key: str, value: Union[str, List, Dict, int, bool]):
        if key in CONFIG_DEFAULT:
            # 设置值
            self.config[key] = value


core_config: CoreConfig = CoreConfig()


class PluginConfigStore:
    """插件独立配置存储

    每个插件的配置存储为 data/plugins_configs/<plugin_name>.json，
    替代原先 config.json["plugins"] 的大字典模式。
    """

    def __init__(self) -> None:
        self._dirty: Set[str] = set()
        self._cache: Dict[str, dict] = {}
        self._migrate_from_config()
        self._load_all()

    def _migrate_from_config(self) -> None:
        """启动时检查 config.json 中是否存在 plugins key，
        如果存在则将每个插件拆分为独立 JSON 文件，然后移除该 key。"""
        if "plugins" not in core_config.config:
            return

        legacy_plugins: Dict[str, dict] = core_config.config["plugins"]
        if not legacy_plugins:
            # 空字典，直接移除
            del core_config.config["plugins"]
            core_config.write_config()
            return

        # 迁移前备份 config.json
        backup_path = CONFIG_PATH.parent / "config_backup.json"
        if not backup_path.exists():
            shutil.copy2(CONFIG_PATH, backup_path)

        for plugin_name, plugin_data in legacy_plugins.items():
            target = PLUGINS_CONFIGS_PATH / f"{plugin_name}.json"
            if not target.exists():
                with open(target, "w", encoding="UTF-8") as f:
                    json.dump(plugin_data, f, indent=4, ensure_ascii=False)

        # 从 config.json 移除 plugins key
        del core_config.config["plugins"]
        core_config.write_config()

    def _load_all(self) -> None:
        """加载 plugins_configs 目录下所有 JSON 文件到内存缓存。"""
        self._cache.clear()
        if not PLUGINS_CONFIGS_PATH.exists():
            return
        for f in PLUGINS_CONFIGS_PATH.iterdir():
            if f.suffix == ".json":
                plugin_name = f.stem
                with open(f, "r", encoding="UTF-8") as fh:
                    self._cache[plugin_name] = json.load(fh)

    def get_all(self) -> Dict[str, dict]:
        """返回所有插件配置的引用（与旧 config_plugins 兼容）。"""
        return self._cache

    def get(self, plugin_name: str) -> dict:
        """获取单个插件配置，不存在则返回空字典。"""
        return self._cache.get(plugin_name, {})

    def set(self, plugin_name: str, data: dict) -> None:
        """设置单个插件配置（内存 + 标记脏）。"""
        self._cache[plugin_name] = data
        self._dirty.add(plugin_name)

    def mark_dirty(self, plugin_name: str) -> None:
        """标记插件配置为脏（仅内存修改后调用，不触发写入）。"""
        self._dirty.add(plugin_name)

    def save(self, plugin_name: str) -> None:
        """持久化单个插件配置到文件。"""
        if plugin_name not in self._cache:
            return
        target = PLUGINS_CONFIGS_PATH / f"{plugin_name}.json"
        max_retries = 3
        retry_delay = 0.5
        for attempt in range(max_retries):
            try:
                with atomic_save(
                    str(target),
                    text_mode=False,
                    overwrite=True,
                    file_perms=0o644,
                ) as file:
                    if file:
                        json_str = json.dumps(
                            self._cache[plugin_name],
                            indent=4,
                            ensure_ascii=False,
                        )
                        file.write(json_str.encode("utf-8"))
                    else:
                        raise RuntimeError(f"写入插件配置文件失败: {plugin_name}")
                self._dirty.discard(plugin_name)
                return
            except OSError:
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    raise

    def save_dirty(self) -> None:
        """持久化所有标记为脏的插件配置。"""
        names = list(self._dirty)
        for name in names:
            self.save(name)

    def save_all(self) -> None:
        """持久化所有插件配置。"""
        for name in list(self._cache):
            self.save(name)


plugin_config_store: PluginConfigStore = PluginConfigStore()
