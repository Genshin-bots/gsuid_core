import json
from pathlib import Path
from typing import Any, Dict, List, Union, Literal, overload

CONFIG_PATH = Path(__file__).parent / 'config.json'

CONFIG_DEFAULT = {
    'HOST': 'localhost',
    'PORT': '8765',
    'ENABLE_HTTP': False,
    'masters': [],
    'superusers': [],
    'misfire_grace_time': 90,
    'log': {
        'level': 'INFO',
        'output': ['stdout', 'stderr', 'file'],
        # ...
    },
    'enable_empty_start': True,
    'command_start': [],
    'sv': {},
    'plugins': {},
}

STR_CONFIG = Literal['HOST', 'PORT']
INT_CONFIG = Literal['misfire_grace_time']
LIST_CONFIG = Literal['superusers', 'masters', 'command_start']
DICT_CONFIG = Literal['sv', 'log', 'plugins']
BOOL_CONFIG = Literal['enable_empty_start', 'ENABLE_HTTP']

plugins_sample = {
    'name': '',
    'pm': 6,
    'priority': 5,
    'enabled': True,
    'area': 'SV',
    'black_list': [],
    'white_list': [],
    'prefix': [],
    'force_prefix': [],
    'disable_force_prefix': False,
    'allow_empty_prefix': False,
    'sv': {},
}


class CoreConfig:
    def __init__(self) -> None:
        if not CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'w', encoding='UTF-8') as file:
                json.dump(CONFIG_DEFAULT, file, indent=4, ensure_ascii=False)

        self.update_config()

    def write_config(self):
        with open(CONFIG_PATH, 'w', encoding='UTF-8') as file:
            json.dump(self.config, file, indent=4, ensure_ascii=False)

    def update_config(self):
        # 打开config.json
        with open(CONFIG_PATH, 'r', encoding='UTF-8') as f:
            self.config: Dict[str, Any] = json.load(f)
        # 对没有的值，添加默认值
        for key in CONFIG_DEFAULT:
            if key not in self.config:
                self.config[key] = CONFIG_DEFAULT[key]
            if isinstance(CONFIG_DEFAULT[key], Dict):
                for sub_key in CONFIG_DEFAULT[key]:
                    if sub_key not in self.config[key]:
                        self.config[key][sub_key] = CONFIG_DEFAULT[key][
                            sub_key
                        ]

        # 重新写回
        self.write_config()

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

    def set_config(
        self, key: str, value: Union[str, List, Dict, int, bool]
    ) -> bool:
        if key in CONFIG_DEFAULT:
            # 设置值
            self.config[key] = value
            # 重新写回
            self.write_config()
            return True
        else:
            return False


core_config = CoreConfig()
