"""工具检索的内存 CJK 二元语法 / 词级 BM25 稀疏检索器。

英文嵌入模型（``bge-small-en-v1.5``）对中文口语查询会大量退化为 ``[UNK]``，
纯 Dense 阈值召回因此漏掉工具。这里对 ``bot_tools`` 候选池建一份内存 BM25
索引，与 Dense 结果做 RRF 融合，保证中文查询仍有稳定词面命中。
"""

from __future__ import annotations

import re
import math
from typing import Mapping, TypeVar, Protocol

_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9_+#.-]{2,}")

_WEIGHT_NAME = 3.0
_WEIGHT_DOMAIN = 2.0
_WEIGHT_COVER = 2.0
_WEIGHT_ALIAS = 2.0
_WEIGHT_DESCRIPTION = 1.0

_BM25_K1 = 1.2
_BM25_B = 0.75


class ToolLexicalDoc(Protocol):
    name: str
    description: str
    capability_domain: str | None
    covers: list[str]
    aliases: list[str]


_T = TypeVar("_T", bound=ToolLexicalDoc)


def has_cjk(text: str) -> bool:
    return _CJK_RUN_RE.search(text or "") is not None


def tokenize(text: str) -> list[str]:
    """中文单字 + 相邻二元语法 + 拉丁/数字词。"""
    body = text or ""
    out: list[str] = []
    for match in _CJK_RUN_RE.finditer(body):
        run = match.group(0)
        out.extend(run)
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    out.extend(m.group(0).lower() for m in _LATIN_RE.finditer(body))
    return out


def _weighted_tokens(tool: ToolLexicalDoc) -> list[str]:
    tokens: list[str] = []
    tokens.extend(tokenize(tool.name) * int(_WEIGHT_NAME))
    domain = tool.capability_domain or ""
    if domain:
        tokens.extend(tokenize(domain) * int(_WEIGHT_DOMAIN))
    for cover in tool.covers:
        tokens.extend(tokenize(cover) * int(_WEIGHT_COVER))
    for alias in tool.aliases:
        tokens.extend(tokenize(alias) * int(_WEIGHT_ALIAS))
    tokens.extend(tokenize(tool.description) * int(_WEIGHT_DESCRIPTION))
    return tokens


class ToolBM25Index:
    """小型内存 BM25 索引（候选池 < 400 个工具）。"""

    def __init__(self) -> None:
        self._tool_names: list[str] = []
        self._doc_tokens: dict[str, list[str]] = {}
        self._doc_len: dict[str, int] = {}
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0

    def rebuild(self, tools: Mapping[str, _T]) -> None:
        self._tool_names = []
        self._doc_tokens = {}
        self._doc_len = {}
        self._df = {}
        total_len = 0
        for name, tool in tools.items():
            tokens = _weighted_tokens(tool)
            if not tokens:
                continue
            self._tool_names.append(name)
            self._doc_tokens[name] = tokens
            self._doc_len[name] = len(tokens)
            total_len += len(tokens)
            for term in set(tokens):
                self._df[term] = (self._df[term] if term in self._df else 0) + 1
        self._avgdl = total_len / len(self._tool_names) if self._tool_names else 0.0

    @property
    def size(self) -> int:
        return len(self._tool_names)

    def scores(self, query: str, allowed: set[str] | None = None) -> dict[str, float]:
        if not self._tool_names:
            return {}
        query_terms = list(dict.fromkeys(tokenize(query)))
        if not query_terms:
            return {}
        n = len(self._tool_names)
        out: dict[str, float] = {}
        for name in self._tool_names:
            if allowed is not None and name not in allowed:
                continue
            tokens = self._doc_tokens[name]
            dl = self._doc_len[name]
            score = 0.0
            for term in query_terms:
                tf = tokens.count(term)
                if tf == 0:
                    continue
                df = self._df[term] if term in self._df else 0
                idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
                denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * dl / (self._avgdl or 1.0))
                score += idf * tf * (_BM25_K1 + 1.0) / denom
            if score > 0:
                out[name] = score
        return out


_INDEX = ToolBM25Index()


def rebuild_index(tools: Mapping[str, _T]) -> None:
    """工具注册表变化（启动同步 / 插件热更）后重建。"""
    _INDEX.rebuild(tools)


def ensure_index(tools: Mapping[str, _T]) -> None:
    """索引为空时按当前候选池惰性建一次（离线启动也能用）。"""
    if _INDEX.size == 0:
        _INDEX.rebuild(tools)


def lexical_scores(query: str, allowed: set[str] | None = None) -> dict[str, float]:
    return _INDEX.scores(query, allowed)


def rrf_fuse(
    dense_names: list[str],
    lexical: dict[str, float],
    *,
    k: int = 60,
    lexical_weight: float = 1.2,
) -> list[str]:
    """Dense 名次 + BM25 名次的 RRF 融合，返回融合后的工具名顺序。"""
    dense_rank = {name: i for i, name in enumerate(dense_names)}
    lex_rank = {name: i for i, name in enumerate(sorted(lexical, key=lambda n: lexical[n], reverse=True))}
    union = list(dict.fromkeys([*dense_names, *lex_rank.keys()]))
    scored: list[tuple[float, str]] = []
    for name in union:
        score = 0.0
        if name in dense_rank:
            score += 1.0 / (k + dense_rank[name])
        if name in lex_rank:
            score += lexical_weight / (k + lex_rank[name])
        scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _score, name in scored]
