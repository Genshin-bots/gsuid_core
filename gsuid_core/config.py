import json
import time
import shutil
import secrets
from typing import Any, Set, Dict, List, Union, Literal, overload
from pathlib import Path

from boltons.fileutils import atomic_save

from gsuid_core.data_store import PLUGINS_CONFIGS_PATH, get_res_path

CONFIG_PATH = get_res_path() / "config.json"
OLD_CONFIG_PATH = Path(__file__).parent / "config.json"


# 生成随机注册码
def _generate_register_code() -> str:
    return secrets.token_hex(16)


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
        # ...
    },
    "enable_empty_start": True,
    "command_start": [],
    "buffered_user_writes": False,
    "sv": {},
}

STR_CONFIG = Literal["HOST", "PORT", "WS_TOKEN", "REGISTER_CODE"]
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
