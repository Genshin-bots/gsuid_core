import json
import shutil
from typing import Any, Dict, List, Union, Literal, overload
from pathlib import Path

from boltons.fileutils import atomic_save

from gsuid_core.data_store import get_res_path

CONFIG_PATH = get_res_path() / "config.json"
OLD_CONFIG_PATH = Path(__file__).parent / "config.json"

CONFIG_DEFAULT = {
    "HOST": "localhost",
    "PORT": "8765",
    "ENABLE_HTTP": False,
    "WS_TOKEN": "",
    "TRUSTED_IPS": ["localhost", "::1", "127.0.0.1"],
    "masters": [],
    "superusers": [],
    "misfire_grace_time": 90,
    "log": {
        "level": "INFO",
        "output": ["stdout", "stderr", "file"],
        "module": False,
        # ...
    },
    "enable_empty_start": True,
    "command_start": [],
    "sv": {},
    "plugins": {},
}

STR_CONFIG = Literal["HOST", "PORT", "WS_TOKEN"]
INT_CONFIG = Literal["misfire_grace_time"]
LIST_CONFIG = Literal["superusers", "masters", "command_start", "TRUSTED_IPS"]
DICT_CONFIG = Literal["sv", "log", "plugins"]
BOOL_CONFIG = Literal["enable_empty_start", "ENABLE_HTTP"]

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

    def lazy_set_config(self, key: str, value: Union[str, List, Dict, int, bool]):
        if key in CONFIG_DEFAULT:
            # 设置值
            self.config[key] = value


core_config: CoreConfig = CoreConfig()
