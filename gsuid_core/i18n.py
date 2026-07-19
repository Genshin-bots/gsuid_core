import json
from enum import Enum
from typing import Dict, List, Optional
from pathlib import Path

from gsuid_core.config import core_config


class Lang(str, Enum):
    """框架 i18n 支持的语言；枚举值即 locales/<value>.json 的文件名。

    用户表 language 字段与全局 LANGUAGE 配置的取值都应落在此集合内。
    """

    ZH_CN = "zh-cn"
    EN = "en"
    JA = "ja"


DEFAULT_LANG: str = Lang.ZH_CN.value
_LOCALES_DIR = Path(__file__).parent / "locales"
_catalogs: Dict[str, Dict[str, str]] = {}


def load_catalogs() -> None:
    """加载 locales/*.json 词条到内存；词条文件变更后可重复调用以热重载。"""
    _catalogs.clear()
    for f in _LOCALES_DIR.glob("*.json"):
        with open(f, "r", encoding="utf-8") as fh:
            _catalogs[f.stem] = json.load(fh)


def supported_langs() -> List[str]:
    """返回当前已加载词条对应的语言列表。"""
    return list(_catalogs)


def is_supported(lang: Optional[str]) -> bool:
    """判定给定语言是否已有词条目录（可作为有效偏好写入）。"""
    return bool(lang) and lang in _catalogs


def get_lang() -> str:
    """当前进程（部署者）语言：读全局 LANGUAGE，非法/缺词条时回落 DEFAULT_LANG。"""
    lang: str = core_config.get_config("LANGUAGE")
    return lang if lang in _catalogs else DEFAULT_LANG


def t(key: str, /, lang: Optional[str] = None, **params: object) -> str:
    """按 key 取词条并以具名占位符填充。

    缺 key 回落 key 本身（显式可见、便于排查），不吞异常、不打断主链路。
    key 为仅限位置参数，避免与同名占位符（如 {key}）冲突。
    """
    use_lang = lang if (lang and lang in _catalogs) else get_lang()
    if use_lang in _catalogs:
        catalog = _catalogs[use_lang]
    elif DEFAULT_LANG in _catalogs:
        catalog = _catalogs[DEFAULT_LANG]
    else:
        catalog = {}
    template = catalog[key] if key in catalog else key
    return template.format(**params) if params else template


load_catalogs()


def lang_display_name(code: str) -> str:
    """某语言的母语自称（取该 locale 的 lang.name 词条）；缺失回落 code 本身。"""
    name = t("lang.name", lang=code)
    return code if name == "lang.name" else name


def available_language_codes() -> List[str]:
    """已加载词条对应的语言 code，按 Lang 枚举顺序（稳定）。

    供 config.py 声明 LANGUAGE 的可选项使用（config 侧运行时局部导入本函数）。
    """
    langs = supported_langs()
    return [lang.value for lang in Lang if lang.value in langs]
