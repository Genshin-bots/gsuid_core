"""GsCore 国际化。

词条布局（框架）::

    gsuid_core/locales/
      zh-cn/
        core.json
        server.json
        ...
      en/
        ...
      ja/
        ...

兼容：若仍存在 ``locales/zh-cn.json`` 单文件，加载时并入（迁移期）。

插件一等公民::

    plugins/FooPlugin/locales/{lang}/*.json
    或 plugins/FooPlugin/locales/{lang}.json

由 ``register_plugin_locales`` 合并进运行时 catalog；**禁止**把插件词条写进框架
``gsuid_core/locales``。框架在插件加载成功后自动扫描 ``locales/`` 目录。

日志 key 约定：``log.<模块>.<语义>``；前导 emoji 由 ``ensure_log_emoji`` 按模块统一装配，
词条 value 可不写 emoji（若已写则不重复加）。
"""

from __future__ import annotations

import re
import json
from enum import Enum
from typing import Set, Dict, List, Tuple, Optional
from pathlib import Path


class Lang(str, Enum):
    """框架 i18n 支持的语言；枚举值即 locales 下目录名 / 历史单文件名。"""

    ZH_CN = "zh-cn"
    EN = "en"
    JA = "ja"


DEFAULT_LANG: str = Lang.ZH_CN.value
_LOCALES_DIR = Path(__file__).parent / "locales"

# lang → 合并后的全量词条
_catalogs: Dict[str, Dict[str, str]] = {}
# lang → bare_key（去 emoji）→ 实际 key
_bare_aliases: Dict[str, Dict[str, str]] = {}
# plugin_name → 其贡献的 key 集合（卸载时用）
_plugin_keys: Dict[str, Set[str]] = {}
# plugin_name → locales 根路径（热重载）
_plugin_locale_roots: Dict[str, Path] = {}

# ── 日志模块 → 统一 emoji（装配层；value 可无前缀）──
LOG_MODULE_EMOJI: Dict[str, str] = {
    "core": "🌱",
    "server": "🔌",
    "handler": "⚡",
    "bot": "🤖",
    "config": "⚙️",
    "database": "🗄️",
    "db": "🗄️",
    "meme": "🎭",
    "memory": "🧠",
    "ai": "🧠",
    "register": "🧠",
    "agent": "🧠",
    "git": "📦",
    "plugin": "🔌",
    "plugins": "🔌",
    "update": "🔄",
    "download": "📥",
    "upload": "☁️",
    "image": "🖼️",
    "webconsole": "🖥️",
    "auth": "🔑",
    "login": "🔑",
    "cookie": "🔑",
    "sign": "✍️",
    "backup": "💾",
    "cache": "📦",
    "aps": "⏰",
    "scheduler": "⏰",
    "trace": "📝",
    "logger": "📝",
    "firewall": "🛡️",
    "output": "🛡️",
    "send": "📤",
    "message": "💬",
    "status": "📊",
    "qdrant": "🗄️",
    "rag": "📚",
    "persona": "🎭",
    "mcp": "🧩",
    "resource": "📥",
    "global_val": "📦",
    "misc": "ℹ️",
    "gscore": "🌱",
    "sv": "⚡",
    "segment": "🧩",
    "security": "🔒",
    "subscribe": "📨",
    "help": "❓",
    "client": "💻",
    "benchmark": "📈",
    "pool": "🏊",
    "mys": "🎮",
    "api": "🌐",
    "genshinuid": "🎮",
    "genshin": "🎮",
    "ai_registry": "🧠",
    "ai_agent": "🧠",
    "resource_download": "📥",
    "git_update": "📦",
    "git_async": "📦",
    "buildin": "🧠",
    "db_admin": "🗄️",
    "entity_index": "🧠",
    "htmlrender": "🖼️",
    "resourcemanager": "📦",
    "ambr": "🎮",
    "upass": "🔑",
    "heartbeat": "🫀",
}

_LEADING_EMOJI_RE = re.compile(
    r"^(?:"
    r"[\U0001F300-\U0001FAFF]"
    r"|[\U0001F000-\U0001F02F]"
    r"|[\U0001F1E0-\U0001F1FF]"
    r"|[\u2600-\u27BF]"
    r"|[\u2300-\u23FF]"
    r"|[\u2190-\u21FF]"
    r"|[\u25A0-\u25FF]"
    r"|[\u2B00-\u2BFF]"
    r"|[\u2100-\u214F]"  # letterlike (ℹ️ …)
    r"|[\u2190-\u21FF]"
    r")"
    r"[\uFE0E\uFE0F]?"
    r"(?:\u200D"
    r"(?:"
    r"[\U0001F300-\U0001FAFF]|[\u2600-\u27BF]"
    r")"
    r"[\uFE0E\uFE0F]?"
    r")*"
    r"\s*"
)


def strip_leading_emoji(text: str) -> str:
    """去掉字符串前导 emoji（及紧随空白）。"""
    if not text:
        return text
    prev = None
    s = text
    for _ in range(2):
        if s == prev:
            break
        prev = s
        s = _LEADING_EMOJI_RE.sub("", s, count=1)
    return s


def starts_with_emoji(text: str) -> bool:
    return bool(text) and strip_leading_emoji(text) != text


def log_module_of_key(key: str) -> Optional[str]:
    """``log.<module>.<semantic>`` → module；否则 None。"""
    if not key.startswith("log."):
        return None
    parts = key.split(".")
    if len(parts) < 3:
        return None
    return parts[1] or None


def emoji_for_log_key(key: str) -> Optional[str]:
    """按 log 模块取统一 emoji。"""
    mod = log_module_of_key(key)
    if not mod:
        return None
    return LOG_MODULE_EMOJI.get(mod.lower()) or LOG_MODULE_EMOJI.get("misc")


def ensure_log_emoji(key: str, text: str) -> str:
    """对 ``log.*`` 文案统一装配模块 emoji；已有前导 emoji 则不重复。"""
    if not key.startswith("log.") or not text:
        return text
    if starts_with_emoji(text):
        return text
    emoji = emoji_for_log_key(key)
    if not emoji:
        return text
    return f"{emoji} {text}"


def _build_bare_aliases(catalog: Dict[str, str]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for key in catalog:
        bare = strip_leading_emoji(key)
        if bare == key or not bare:
            continue
        existing = aliases.get(bare)
        if existing is None:
            aliases[bare] = key
            continue
        if strip_leading_emoji(existing) == existing and strip_leading_emoji(key) != key:
            aliases[bare] = key
    return aliases


def _rebuild_aliases_for_lang(lang: str) -> None:
    cat = _catalogs.get(lang)
    if cat is not None:
        _bare_aliases[lang] = _build_bare_aliases(cat)


def _load_lang_catalog_from_dir(lang_dir: Path) -> Dict[str, str]:
    """合并 ``lang_dir/*.json``（忽略子目录与非 json）。"""
    merged: Dict[str, str] = {}
    if not lang_dir.is_dir():
        return merged
    for f in sorted(lang_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, str):
                merged[k] = v
    return merged


def _load_lang_catalog(lang: str) -> Dict[str, str]:
    """优先目录 ``locales/<lang>/``，否则回落单文件 ``locales/<lang>.json``。"""
    dir_path = _LOCALES_DIR / lang
    merged = _load_lang_catalog_from_dir(dir_path)
    single = _LOCALES_DIR / f"{lang}.json"
    if single.is_file():
        try:
            data = json.loads(single.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str) and k not in merged:
                    merged[k] = v
    return merged


def load_catalogs() -> None:
    """加载框架词条；保留已注册的插件词条并重合并。"""
    plugin_snapshot: Dict[str, Path] = dict(_plugin_locale_roots)
    _catalogs.clear()
    _bare_aliases.clear()
    _plugin_keys.clear()
    _plugin_locale_roots.clear()

    # 发现语言：子目录名 + 单文件 stem
    langs: Set[str] = set()
    if _LOCALES_DIR.is_dir():
        for p in _LOCALES_DIR.iterdir():
            if p.is_dir() and not p.name.startswith((".", "_")):
                langs.add(p.name)
            elif p.is_file() and p.suffix == ".json":
                langs.add(p.stem)
    if not langs:
        langs = {lang.value for lang in Lang}

    for lang in sorted(langs):
        _catalogs[lang] = _load_lang_catalog(lang)
        _rebuild_aliases_for_lang(lang)

    # 重放插件注册
    for name, root in plugin_snapshot.items():
        register_plugin_locales(name, root, reload=True)


def supported_langs() -> List[str]:
    return list(_catalogs)


def is_supported(lang: Optional[str]) -> bool:
    return bool(lang) and lang in _catalogs


def get_lang() -> str:
    # 延迟导入：避免 import i18n 时拉起 config（boltons 等运行时依赖），
    # 使 CI 中仅装 pytest 的静态/轻量 i18n 测试可直接 import。
    from gsuid_core.config import core_config

    lang: str = core_config.get_config("LANGUAGE")
    return lang if lang in _catalogs else DEFAULT_LANG


def _lookup_template(catalog: Dict[str, str], aliases: Dict[str, str], key: str) -> str:
    if key in catalog:
        return catalog[key]
    bare = strip_leading_emoji(key)
    if bare != key and bare in catalog:
        return catalog[bare]
    aliased = aliases.get(key) or (aliases.get(bare) if bare != key else None)
    if aliased is not None and aliased in catalog:
        return catalog[aliased]
    return key


def t(key: str, /, lang: Optional[str] = None, **params: object) -> str:
    """按 key 取词条并以具名占位符填充。

    - 缺 key 回落 key 本身。
    - ``log.*`` 结果经 ``ensure_log_emoji`` 按模块统一装配前导 emoji。
    - 插件词条经 ``register_plugin_locales`` 合并后与框架同一 ``t()`` 查找。
    """
    use_lang = lang if (lang and lang in _catalogs) else get_lang()
    if use_lang in _catalogs:
        catalog = _catalogs[use_lang]
        aliases = _bare_aliases.get(use_lang, {})
    elif DEFAULT_LANG in _catalogs:
        catalog = _catalogs[DEFAULT_LANG]
        aliases = _bare_aliases.get(DEFAULT_LANG, {})
    else:
        catalog = {}
        aliases = {}
    template = _lookup_template(catalog, aliases, key)
    text = template.format(**params) if params else template
    return ensure_log_emoji(key, text)


def _iter_plugin_locale_files(locales_root: Path) -> List[Tuple[str, Path]]:
    """返回 (lang, json_file) 列表。

    支持::
        locales/zh-cn.json
        locales/zh-cn/*.json
        locales/zh-cn/foo.json
    """
    out: List[Tuple[str, Path]] = []
    if not locales_root.is_dir():
        return out
    for p in sorted(locales_root.iterdir()):
        if p.is_file() and p.suffix == ".json":
            out.append((p.stem, p))
        elif p.is_dir() and not p.name.startswith((".", "_")):
            lang = p.name
            for f in sorted(p.glob("*.json")):
                out.append((lang, f))
    return out


def register_plugin_locales(
    plugin_name: str,
    locales_root: Path | str,
    *,
    reload: bool = False,
    override_framework: bool = False,
) -> int:
    """合并插件词条到运行时 catalog。

    - 默认**不覆盖**框架已有 key（冲突跳过）。
    - ``reload=True`` 时先卸载该插件旧 key 再注册。
    - 返回成功写入的 key 条数（跨语言合计，同一 key 多语言计多次）。

    插件词条**不得**提交进 ``gsuid_core/locales``；仅放在插件目录下由本函数摄入。
    """
    root = Path(locales_root)
    if reload and plugin_name in _plugin_keys:
        unregister_plugin_locales(plugin_name)

    written = 0
    contributed: Set[str] = set(_plugin_keys.get(plugin_name, set()))
    files = _iter_plugin_locale_files(root)
    if not files:
        return 0

    for lang, fpath in files:
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if lang not in _catalogs:
            _catalogs[lang] = {}
        cat = _catalogs[lang]
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                continue
            if k in cat and not override_framework:
                # 框架 key 优先；同插件 reload 时 key 已卸掉
                if k not in contributed:
                    continue
            cat[k] = v
            contributed.add(k)
            written += 1
        _rebuild_aliases_for_lang(lang)

    _plugin_keys[plugin_name] = contributed
    _plugin_locale_roots[plugin_name] = root
    return written


def unregister_plugin_locales(plugin_name: str) -> None:
    """移除某插件贡献的全部 key（重载/卸载用）。"""
    keys = _plugin_keys.pop(plugin_name, None)
    _plugin_locale_roots.pop(plugin_name, None)
    if not keys:
        return
    for lang, cat in _catalogs.items():
        for k in keys:
            cat.pop(k, None)
        _rebuild_aliases_for_lang(lang)


def discover_and_register_plugin_locales(plugin_dir: Path | str, plugin_name: Optional[str] = None) -> int:
    """若 ``plugin_dir/locales`` 存在则注册。``plugin_name`` 默认取目录名。"""
    pdir = Path(plugin_dir)
    name = plugin_name or pdir.name
    locales = pdir / "locales"
    if not locales.is_dir():
        return 0
    return register_plugin_locales(name, locales, reload=True)


def plugin_locale_key_count(plugin_name: str) -> int:
    return len(_plugin_keys.get(plugin_name, ()))


load_catalogs()


def lang_display_name(code: str) -> str:
    name = t("lang.name", lang=code)
    return code if name == "lang.name" else name


def available_language_codes() -> List[str]:
    langs = supported_langs()
    return [lang.value for lang in Lang if lang.value in langs]
