import sys
import json
from typing import Any, Dict, List, Union
from pathlib import Path

from msgspec import ValidationError, json as msgjson, to_builtins
from boltons.fileutils import atomic_save

from gsuid_core.logger import logger
from gsuid_core.data_store import RES, CONFIGS_PATH

from .models import (
    GSC,
    GsStrConfig,
    GsBoolConfig,
    GsDictConfig,
    GsTimeRConfig,
    GsListStrConfig,
)
from .sp_config import SP_CONIFG
from .log_config import LOG_CONFIG
from .pass_config import PASS_CONIFG_DEFAULT
from .backup_config import BACKUP_CONFIG
from .status_config import STATUS_CONIFG
from .config_default import CONIFG_DEFAULT
from .pic_gen_config import PIC_GEN_CONIFG
from .database_config import DATABASE_CONIFG
from .send_pic_config import SEND_PIC_CONIFG
from .pic_server_config import PIC_UPLOAD_CONIFG
from .buttons_and_markdown_config import BM_CONIFG_DEFAULT


class StringConfig:
    def __new__(cls, *args, **kwargs):
        # 判断sv是否已经被初始化
        if len(args) >= 1:
            name = args[0]
        else:
            name = kwargs.get("config_name")

        if name is None:
            raise ValueError("Config.name is None!")

        if name in all_config_list:
            return all_config_list[name]
        else:
            _config = super().__new__(cls)
            all_config_list[name] = _config
            return _config

    def __init__(
        self,
        config_name: str,
        CONFIG_PATH: Union[Path, List[Path]],
        config_list: Dict[str, GSC],
    ) -> None:
        """
        初始化配置文件管理器。

        Args:
            config_name: 配置名称，用于标识配置项。
            CONFIG_PATH: 配置文件路径，可以是单个路径或路径列表。
                         当为列表时，将按照以下逻辑处理：
                         1. 首先检查列表中除最后一个路径外的其他路径是否存在配置文件
                         2. 如果在 [0:-1] 范围内的任何路径找到配置文件，
                            会将其安全迁移到最后一个路径（index=-1）所指向的位置
                         3. 最终加载和使用的配置文件始终是 index=-1 指向的路径
                         4. 迁移过程中，原有配置文件会被读取后写入新位置，然后删除
                         5. 如果目标路径已存在配置文件，则不会进行迁移操作
            config_list: 配置项的默认字典，用于初始化或校验配置。

        Example:
            # 单路径方式
            CONFIG_PATH = Path("config.json")

            # 多路径方式 - 会从 path1 迁移到最终路径
            CONFIG_PATH = [Path("old_config.json"), Path("new_config.json")]
        """
        self.config_list = config_list
        self.config_default = config_list
        self.config_name = config_name
        self.CONFIG_PATH: Path = None  # type: ignore

        if isinstance(CONFIG_PATH, list):
            # 按照 [0:-1] 的顺序查找是否存在配置文件
            for old_path in CONFIG_PATH[:-1]:
                if old_path.exists():
                    final_path = CONFIG_PATH[-1]
                    # 安全迁移：将找到的配置文件迁移到最终路径
                    self._migrate_config(old_path, final_path)
                    break
            # 最终配置文件路径始终为列表的最后一个元素
            CONFIG_PATH = CONFIG_PATH[-1]

        if not CONFIG_PATH.exists():
            with open(CONFIG_PATH, "wb") as file:
                file.write(msgjson.encode(config_list))

        self.CONFIG_PATH = CONFIG_PATH
        self.config: Dict[str, GSC] = {}  # type: ignore
        # 获取调用者的插件名
        self.plugin_name = self._get_caller_plugin_name()
        self.update_config()

    def _migrate_config(self, old_path: Path, new_path: Path) -> None:
        """
        安全地将配置文件从旧路径迁移到新路径。

        只有当新路径不存在配置文件时，才会进行迁移操作。
        迁移过程：读取旧配置 -> 写入新路径 -> 删除旧文件

        Args:
            old_path: 旧的配置文件路径。
            new_path: 新的配置文件路径。
        """
        # 如果新路径已存在配置文件，则不进行迁移
        if new_path.exists():
            logger.info(f"[配置][{self.config_name}] 目标配置文件已存在，跳过从 {old_path} 迁移")
            return

        # 确保目标目录存在
        new_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # 读取旧配置文件内容
            with open(old_path, "r", encoding="UTF-8") as f:
                content = f.read()

            # 写入新路径
            with open(new_path, "w", encoding="UTF-8") as f:
                f.write(content)

            # 删除旧文件
            old_path.unlink()

            logger.info(f"[配置][{self.config_name}] 配置文件已从 {old_path} 迁移到 {new_path}")
        except Exception as e:
            logger.error(f"[配置][{self.config_name}] 配置文件迁移失败: {e}")

    def _get_caller_plugin_name(self):
        try:
            frame = sys._getframe(2)
            parts = Path(frame.f_code.co_filename).resolve().parts

            # 从后往前查找 gsuid_core/plugins
            for i in range(len(parts) - 2, 0, -1):
                if parts[i - 1] == "gsuid_core" and parts[i] == "plugins":
                    # 返回 plugins 的下一级目录名
                    return parts[i + 1] if i + 1 < len(parts) else None

        except ValueError:
            # 栈层级不够，getframe(2)失败
            return None

    def __len__(self):
        return len(self.config)

    def __iter__(self):
        return iter(self.config)

    def __getitem__(self, key) -> GSC:
        return self.config[key]

    def get_raw_config(self) -> Dict:
        return to_builtins(self.config)

    def sort_config(self):
        _config = {}

        for i in self.config_list:
            if i in self.config:
                _config[i] = self.config[i]

        for i in self.config:
            if i not in _config:
                _config[i] = self.config[i]

        self.config = _config

        self.write_config()

    def write_config(self):
        with atomic_save(
            str(self.CONFIG_PATH),
            text_mode=False,
            overwrite=True,
            file_perms=0o644,
        ) as file:
            if file:
                file.write(msgjson.format(msgjson.encode(self.config), indent=4))
            else:
                logger.error("写入配置文件失败!")

    def repair_config(self):
        with open(self.CONFIG_PATH, "r", encoding="UTF-8") as f:
            logger.warning(f"[配置][{self.config_name}] 配置文件格式有变动, 已重置...")
            # 打开self.CONFIG_PATH，用json加载
            temp_config: Dict[str, Dict[str, Any]] = json.load(f)

            for key in self.config_list:
                defalut_dict = to_builtins(self.config_list[key])
                if key not in temp_config:
                    continue
                if list(temp_config[key].keys()) == list(defalut_dict.keys()):
                    continue
                else:
                    temp_config[key] = defalut_dict

        with open(self.CONFIG_PATH, "w", encoding="UTF-8") as f:
            json.dump(temp_config, f, indent=4, ensure_ascii=False)

    def update_config(self):
        is_error = False
        # 打开config.json
        try:
            with open(self.CONFIG_PATH, "r", encoding="UTF-8") as f:
                d = f.read()
        except UnicodeDecodeError:
            with open(self.CONFIG_PATH, "r") as f:
                d = f.read()

        try:
            self.config: Dict[str, GSC] = msgjson.decode(
                d,
                type=Dict[str, GSC],
            )
        except ValidationError:
            self.repair_config()
            is_error = True

        if is_error:
            self.update_config()
            return

        # 对没有的值，添加默认值
        for key in self.config_list:
            _defalut = self.config_list[key]
            if key not in self.config:
                self.config[key] = _defalut
            else:
                # 检查配置项类型是否一致
                stored_type = type(self.config[key])
                expected_type = type(_defalut)

                if stored_type != expected_type:
                    logger.warning(
                        f"[配置][{self.config_name}] 配置项 {key} 类型不一致 "
                        f"({stored_type.__name__} -> {expected_type.__name__}), "
                        f"已重置为默认值"
                    )
                    self.config[key] = _defalut
                else:
                    if isinstance(_defalut, GsStrConfig) or isinstance(_defalut, GsListStrConfig):
                        self.config[key].options = _defalut.options  # type: ignore

                    self.config[key].title = _defalut.title
                    self.config[key].desc = _defalut.desc

        """
        # 对默认值没有的值，直接删除
        delete_keys = []
        for key in self.config:
            if key not in self.config_list:
                delete_keys.append(key)
        for key in delete_keys:
            self.config.pop(key)
        """

        # 重新写回
        self.sort_config()

    def get_config(self, key: str, default_value: Any = None) -> Any:
        if key in self.config:
            return self.config[key]
        elif key in self.config_list:
            logger.info(f"[配置][{self.config_name}] 配置项 {key} 不存在, 但是默认配置存在, 已更新...")
            self.update_config()
            return self.config[key]
        else:
            logger.warning(f"[配置][{self.config_name}] 配置项 {key} 不存在也没有配置, 返回默认参数...")
            if default_value is None:
                return GsBoolConfig("缺省值", "获取错误的配置项", False)

            if isinstance(default_value, str):
                return GsStrConfig("缺省值", "获取错误的配置项", default_value)
            elif isinstance(default_value, bool):
                return GsBoolConfig("缺省值", "获取错误的配置项", default_value)
            elif isinstance(default_value, List):
                return GsListStrConfig("缺省值", "获取错误的配置项", default_value)
            elif isinstance(default_value, Dict):
                return GsDictConfig("缺省值", "获取错误的配置项", default_value)
            else:
                return GsBoolConfig("缺省值", "获取错误的配置项", False)

    def set_config(self, key: str, value: Union[str, List, bool, Dict]) -> bool:
        if key in self.config_list:
            temp = self.config[key].data
            if type(value) == type(temp):  # noqa: E721
                # 设置值
                self.config[key].data = value  # type: ignore
                # 重新写回
                self.write_config()
                return True
            elif isinstance(self.config[key], GsTimeRConfig) and isinstance(value, list):
                # GsTimeRConfig 接受 list 类型并转换为 tuple
                self.config[key].data = tuple(value)  # type: ignore
                self.write_config()
                return True
            else:
                logger.warning(f"[配置][{self.config_name}] 配置项 {key} 写入类型不正确, 停止写入...")
                return False
        else:
            return False

    def migrate_from(self, old_configs: Union["StringConfig", List["StringConfig"]]):
        """
        自动从旧配置实例中吸取同名键的数据（仅迁移用户设定的 data 值，保留新配置的 title/desc）。
        完成后自动从旧文件中清理该键。
        """
        if not isinstance(old_configs, list):
            old_configs = [old_configs]

        changed = False

        for old_config in old_configs:
            old_changed = False

            # 遍历当前新配置合法的所有键
            for key in self.config_list:
                # 如果旧配置里有这个键
                if key in old_config.config:
                    # 仅转移最核心的 data 用户数据，这样就不会覆盖新配置的文本描述
                    self.config[key].data = old_config.config[key].data  # type: ignore
                    changed = True

                    # 既然转移成功了，就从旧配置里物理删除它
                    old_config.config.pop(key)
                    old_changed = True
                    logger.info(f"[配置迁移] 已将配置项 [{key}] 从 {old_config.config_name} 转移至 {self.config_name}")

            # 如果旧配置被掏空了某些键，保存旧配置
            if old_changed:
                old_config.sort_config()

        # 如果新配置吸收了数据，保存新配置
        if changed:
            self.sort_config()


all_config_list: Dict[str, StringConfig] = {}

core_plugins_config = StringConfig(
    "GsCore",
    [
        RES / "core_config.json",
        CONFIGS_PATH / "core_config.json",
    ],
    CONIFG_DEFAULT,
)

pic_upload_config = StringConfig(
    "GsCore图片上传",
    [
        RES / "pic_upload_config.json",
        CONFIGS_PATH / "pic_upload_config.json",
    ],
    PIC_UPLOAD_CONIFG,
)

send_pic_config = StringConfig(
    "GsCore发送图片",
    [
        RES / "send_pic_config.json",
        CONFIGS_PATH / "send_pic_config.json",
    ],
    SEND_PIC_CONIFG,
)

log_config = StringConfig(
    "GsCore日志配置",
    [
        RES / "log_config.json",
        CONFIGS_PATH / "log_config.json",
    ],
    LOG_CONFIG,
)

pic_gen_config = StringConfig(
    "GsCore图片生成",
    [
        RES / "pic_gen_config.json",
        CONFIGS_PATH / "pic_gen_config.json",
    ],
    PIC_GEN_CONIFG,
)

sp_config = StringConfig(
    "GsCore杂项配置",
    [
        RES / "sp_config.json",
        CONFIGS_PATH / "sp_config.json",
    ],
    SP_CONIFG,
)

database_config = StringConfig(
    "GsCore数据库配置",
    [
        RES / "database_config.json",
        CONFIGS_PATH / "database_config.json",
    ],
    DATABASE_CONIFG,
)

status_config = StringConfig(
    "GsCore状态配置",
    [
        RES / "status_config.json",
        CONFIGS_PATH / "status_config.json",
    ],
    STATUS_CONIFG,
)

backup_config = StringConfig(
    "GsCore备份配置",
    [
        RES / "backup_config.json",
        CONFIGS_PATH / "backup_config.json",
    ],
    BACKUP_CONFIG,
)

pass_config = StringConfig(
    "GsCore验证配置",
    CONFIGS_PATH / "pass_config.json",
    PASS_CONIFG_DEFAULT,
)

bm_config = StringConfig(
    "GsCore按钮和MD配置",
    CONFIGS_PATH / "bm_config.json",
    BM_CONIFG_DEFAULT,
)


pass_config.migrate_from(core_plugins_config)
pic_upload_config.migrate_from(core_plugins_config)
bm_config.migrate_from([core_plugins_config, sp_config])
sp_config.migrate_from(core_plugins_config)
