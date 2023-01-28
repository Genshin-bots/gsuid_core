import json
from pathlib import Path
from typing import Dict, List, Union, Literal, overload

CONFIG_PATH = Path(__file__).parent / 'config.json'

CONIFG_DEFAULT = {
    'HOST': 'localhost',
    'PORT': '8765',
    'superusers': [],
    'sv': {},
}
STR_CONFIG = Literal['HOST', 'PORT']
LIST_CONFIG = Literal['superusers']
DICT_CONFIG = Literal['sv']


class CoreConfig:
    def __init__(self) -> None:
        if not CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'w', encoding='UTF-8') as file:
                json.dump(CONIFG_DEFAULT, file, indent=4, ensure_ascii=False)

        self.update_config()

    def write_config(self):
        with open(CONFIG_PATH, 'w', encoding='UTF-8') as file:
            json.dump(self.config, file, indent=4, ensure_ascii=False)

    def update_config(self):
        # 打开config.json
        with open(CONFIG_PATH, 'r', encoding='UTF-8') as f:
            self.config = json.load(f)
        # 对没有的值，添加默认值
        for key in CONIFG_DEFAULT:
            if key not in self.config:
                self.config[key] = CONIFG_DEFAULT[key]

        # 重新写回
        self.write_config()

    @overload
    def get_config(self, key: STR_CONFIG) -> str:
        ...

    @overload
    def get_config(self, key: DICT_CONFIG) -> Dict:
        ...

    @overload
    def get_config(self, key: LIST_CONFIG) -> List:
        ...

    def get_config(self, key: str) -> Union[str, Dict, List]:
        if key in self.config:
            return self.config[key]
        elif key in CONIFG_DEFAULT:
            self.update_config()
            return self.config[key]
        else:
            return {}

    @overload
    def set_config(self, key: STR_CONFIG, value: str) -> bool:
        ...

    @overload
    def set_config(self, key: LIST_CONFIG, value: List) -> bool:
        ...

    @overload
    def set_config(self, key: DICT_CONFIG, value: Dict) -> bool:
        ...

    def set_config(self, key: str, value: Union[str, List, Dict]) -> bool:
        if key in CONIFG_DEFAULT:
            # 设置值
            self.config[key] = value
            # 重新写回
            self.write_config()
            return True
        else:
            return False


core_config = CoreConfig()
