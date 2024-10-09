from pathlib import Path
from typing import Any, Dict, List, Union

from msgspec import json as msgjson

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

from .sp_config import SP_CONIFG
from .config_default import CONIFG_DEFAULT
from .pic_gen_config import PIC_GEN_CONIFG
from .database_config import DATABASE_CONIFG
from .security_config import SECURITY_CONFIG
from .send_pic_config import SEND_PIC_CONIFG
from .pic_server_config import PIC_UPLOAD_CONIFG
from .models import (
    GSC,
    GsStrConfig,
    GsBoolConfig,
    GsDictConfig,
    GsListStrConfig,
)

RES = get_res_path()


class StringConfig:
    def __new__(cls, *args, **kwargs):
        # 判断sv是否已经被初始化
        if len(args) >= 1:
            name = args[0]
        else:
            name = kwargs.get('config_name')

        if name is None:
            raise ValueError('Config.name is None!')

        if name in all_config_list:
            return all_config_list[name]
        else:
            _config = super().__new__(cls)
            all_config_list[name] = _config
            return _config

    def __init__(
        self, config_name: str, CONFIG_PATH: Path, config_list: Dict[str, GSC]
    ) -> None:
        self.config_list = config_list

        if not CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'wb') as file:
                file.write(msgjson.encode(config_list))

        self.config_name = config_name
        self.CONFIG_PATH = CONFIG_PATH
        self.config: Dict[str, GSC] = {}  # type: ignore
        self.update_config()

    def __len__(self):
        return len(self.config)

    def __iter__(self):
        return iter(self.config)

    def __getitem__(self, key) -> GSC:
        return self.config[key]

    def sort_config(self):
        _config = {}
        for i in self.config_list:
            _config[i] = self.config[i]
        self.config = _config

        self.write_config()

    def write_config(self):
        with open(self.CONFIG_PATH, 'wb') as file:
            file.write(msgjson.format(msgjson.encode(self.config), indent=4))

    def update_config(self):
        # 打开config.json
        with open(self.CONFIG_PATH, 'r', encoding='UTF-8') as f:
            self.config: Dict[str, GSC] = msgjson.decode(
                f.read(),
                type=Dict[str, GSC],
            )

        # 对没有的值，添加默认值
        for key in self.config_list:
            _defalut = self.config_list[key]
            if key not in self.config:
                self.config[key] = _defalut
            else:
                if isinstance(_defalut, GsStrConfig) or isinstance(
                    _defalut, GsListStrConfig
                ):
                    self.config[key].options = _defalut.options  # type: ignore

        # 对默认值没有的值，直接删除
        delete_keys = []
        for key in self.config:
            if key not in self.config_list:
                delete_keys.append(key)
        for key in delete_keys:
            self.config.pop(key)

        # 重新写回
        self.sort_config()

    def get_config(self, key: str, default_value: Any = None) -> Any:
        if key in self.config:
            return self.config[key]
        elif key in self.config_list:
            logger.info(
                f'[配置][{self.config_name}] 配置项 {key} 不存在, 但是默认配置存在, 已更新...'
            )
            self.update_config()
            return self.config[key]
        else:
            logger.warning(
                f'[配置][{self.config_name}] 配置项 {key} 不存在也没有配置, 返回默认参数...'
            )
            if default_value is None:
                return GsBoolConfig('缺省值', '获取错误的配置项', False)

            if isinstance(default_value, str):
                return GsStrConfig('缺省值', '获取错误的配置项', default_value)
            elif isinstance(default_value, bool):
                return GsBoolConfig(
                    '缺省值', '获取错误的配置项', default_value
                )
            elif isinstance(default_value, List):
                return GsListStrConfig(
                    '缺省值', '获取错误的配置项', default_value
                )
            elif isinstance(default_value, Dict):
                return GsDictConfig(
                    '缺省值', '获取错误的配置项', default_value
                )
            else:
                return GsBoolConfig('缺省值', '获取错误的配置项', False)

    def set_config(
        self, key: str, value: Union[str, List, bool, Dict]
    ) -> bool:
        if key in self.config_list:
            temp = self.config[key].data
            if type(value) == type(temp):  # noqa: E721
                # 设置值
                self.config[key].data = value  # type: ignore
                # 重新写回
                self.write_config()
                return True
            else:
                logger.warning(
                    f'[配置][{self.config_name}] 配置项 {key} 写入类型不正确, 停止写入...'
                )
                return False
        else:
            return False


all_config_list: Dict[str, StringConfig] = {}

core_plugins_config = StringConfig(
    'Core',
    RES / 'core_config.json',
    CONIFG_DEFAULT,
)

pic_upload_config = StringConfig(
    'GsCore图片上传',
    RES / 'pic_upload_config.json',
    PIC_UPLOAD_CONIFG,
)

send_pic_config = StringConfig(
    'GsCore发送图片',
    RES / 'send_pic_config.json',
    SEND_PIC_CONIFG,
)

pic_gen_config = StringConfig(
    'GsCore图片生成',
    RES / 'pic_gen_config.json',
    PIC_GEN_CONIFG,
)

send_security_config = StringConfig(
    'GsCore消息检查处理',
    RES / 'send_security_config.json',
    SECURITY_CONFIG,
)

sp_config = StringConfig(
    'GsCore杂项配置',
    RES / 'sp_config.json',
    SP_CONIFG,
)

database_config = StringConfig(
    'GsCore数据库配置',
    RES / 'database_config.json',
    DATABASE_CONIFG,
)
