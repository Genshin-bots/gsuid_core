"""实体身份索引：surface（正式名 / 别名）→ 所属插件的唯一真值源。

`ai_entity`（带 content 的知识文档，进 Qdrant 向量库）与 `ai_alias`（别名词表，
精确匹配、零嵌入）是**两种存储、两种检索方式，不应合并**——把上千条别名灌进向量库
只会污染 knowledge 召回并白烧嵌入成本。但它们都在声明同一件事：
**"这个词指的是哪个实体、属于哪个插件"**。本模块把这份**身份**抽出来单独维护。

为什么需要它：工具检索靠嵌入做「实体 → 插件」的路由是**方向性错误**——
"玄翎秧秧属于鸣潮"是世界知识，不是文本相似度。生产事故里，嵌入把
鸣潮/原神角色的面板请求路由到了异环插件。本索引让这一步变成**确定性查表**。

## 安全约束（宁可漏，不可错）

误路由的代价是把无关插件的工具塞进本轮工具池，挤掉真正的语义种子。因此：

- **短词不入索引**：CJK < 2 字、ASCII < 3 字一律丢弃。真实注册表里存在
  `日` `月` `春` `夏` `仇` `竹` 这样的单字别名，以及 `xx` `dj` `ly` `jk` 这样的
  ASCII 缩写——不设防会让**几乎每条消息**都误命中。
- **ASCII 需词边界**：避免 `lbk` 命中 `flbkx`。
- **歧义不路由**：一个 surface 被多个插件注册时保留全部候选，并标记
  `is_ambiguous`；调用方**必须**据此退让回普通向量检索，不得强行二选一。
"""

import re
from typing import Dict, List, Tuple, Optional
from dataclasses import field, dataclass

from gsuid_core.logger import logger

# CJK surface 至少 2 字、ASCII surface 至少 3 字才允许入索引（见模块 docstring）。
_MIN_CJK_LEN: int = 2
_MIN_ASCII_LEN: int = 3


@dataclass
class EntityRef:
    """一个 surface（正式名或别名）的身份解析结果。"""

    surface: str
    canonicals: List[str] = field(default_factory=list)
    plugins: List[str] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        """被多个插件注册 = 无法确定归属，调用方不得据此路由。"""
        return len(self.plugins) > 1


# surface（已归一化）→ EntityRef
_SURFACE_INDEX: Dict[str, EntityRef] = {}
# 按长度降序排好的 surface 列表，None 表示需要重建（注册期频繁写入，查询期才排序）
_SCAN_ORDER: Optional[List[str]] = None


def _normalize_surface(surface: str) -> str:
    """索引与查询共用的归一化：去空白 + ASCII 小写（CJK 不受影响）。"""
    return surface.strip().lower()


def _is_indexable(surface: str) -> bool:
    """短词一律拒绝入索引——它们是误路由的主要来源（见模块 docstring）。"""
    if not surface:
        return False
    if surface.isascii():
        return len(surface) >= _MIN_ASCII_LEN and surface.isalnum()
    return len(surface) >= _MIN_CJK_LEN


def register_entity_surface(surface: str, canonical: str, plugin: str) -> None:
    """把一个 surface 登记到身份索引（由 `ai_alias` / 插件调用，幂等）。"""
    global _SCAN_ORDER

    key = _normalize_surface(surface)
    if not _is_indexable(key):
        return
    if not plugin or plugin == "unknown":
        return

    if key in _SURFACE_INDEX:
        ref = _SURFACE_INDEX[key]
    else:
        ref = EntityRef(surface=key)
        _SURFACE_INDEX[key] = ref
        _SCAN_ORDER = None

    if canonical and canonical not in ref.canonicals:
        ref.canonicals.append(canonical)
    if plugin not in ref.plugins:
        ref.plugins.append(plugin)


def lookup_surface(surface: str) -> Optional[EntityRef]:
    """精确查一个 surface 的身份；未注册返回 None。"""
    key = _normalize_surface(surface)
    if key not in _SURFACE_INDEX:
        return None
    return _SURFACE_INDEX[key]


def _scan_order() -> List[str]:
    """按长度降序的 surface 扫描序——保证"玄翎秧秧"优先于"秧秧"命中。"""
    global _SCAN_ORDER
    if _SCAN_ORDER is None:
        _SCAN_ORDER = sorted(_SURFACE_INDEX, key=len, reverse=True)
    return _SCAN_ORDER


def _contains(text: str, surface: str) -> bool:
    """CJK 直接子串匹配；ASCII 要求两侧不是 ASCII 字母数字，避免 `lbk` 命中 `flbkx`。

    **不能用 `\\b`**：Python 正则的 `\\w` 把 CJK 也算作单词字符，于是 `\\btartaglia\\b`
    在 "tartaglia面板" 里**匹配不上**（a 与 面 之间无边界）——所有"英文别名 + 中文"
    的提问都会静默漏掉路由。这里用显式的 ASCII 字符类断言。
    """
    if surface.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(surface)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return surface in text


def find_entities_in_text(text: str, max_hits: int = 8) -> List[EntityRef]:
    """扫描文本里出现的已注册实体，按 surface 长度降序、去重后返回。

    长 surface 命中后，其覆盖到的短 surface 不再重复计入（"玄翎秧秧" 吃掉 "秧秧"）。
    """
    if not text or not _SURFACE_INDEX:
        return []

    lowered = _normalize_surface(text)
    hits: List[EntityRef] = []
    matched_spans: List[Tuple[int, int]] = []

    for surface in _scan_order():
        if len(hits) >= max_hits:
            break
        if not _contains(lowered, surface):
            continue
        start = lowered.find(surface)
        end = start + len(surface)
        # 已被更长的命中覆盖 → 同一个实体，不重复计入
        if any(s <= start and end <= e for s, e in matched_spans):
            continue
        matched_spans.append((start, end))
        hits.append(_SURFACE_INDEX[surface])

    return hits


def plugins_in_text(text: str) -> List[str]:
    """文本里**无歧义**命中的插件列表（歧义 surface 直接跳过，绝不猜）。

    返回空列表 = 没有可靠的实体信号，调用方应老实走普通向量检索。
    """
    plugins: List[str] = []
    for ref in find_entities_in_text(text):
        if ref.is_ambiguous:
            logger.trace(f"🧠 [EntityIndex] surface {ref.surface!r} 归属歧义 {ref.plugins}，不路由")
            continue
        for plugin in ref.plugins:
            if plugin not in plugins:
                plugins.append(plugin)
    return plugins


def get_entity_index() -> Dict[str, EntityRef]:
    """全量索引（供评测集生成 / 调试用，勿在热路径遍历）。"""
    return _SURFACE_INDEX


def clear_entity_index() -> None:
    """清空索引（插件热重载 / 测试用）。"""
    global _SCAN_ORDER
    _SURFACE_INDEX.clear()
    _SCAN_ORDER = None
