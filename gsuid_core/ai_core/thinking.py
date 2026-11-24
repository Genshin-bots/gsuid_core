"""思考标签流式拆分与内嵌思考去重（纯标准库顶层叶子层）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Set, List, Sequence

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelResponsePart


def _normalize_thinking_tags(thinking_tags: tuple[str, str]) -> tuple[str, str]:
    """把 profile 里的裸标签名补成成对尖括号。

    pydantic_ai 的 ``DEFAULT_THINKING_TAGS`` 是 ``('<think>', '</think>')``；
    我们曾把缺省写成 ``('think', 'think')``。裸名直接 ``str.find`` 会命中英文
    思考里的单词 think，把正文拆碎。已是 ``<…>`` / ``</…>`` 的原样返回。
    """
    start, end = thinking_tags
    if start and not start.startswith("<"):
        start = f"<{start}>"
    if end and not end.startswith("<"):
        end = f"</{end}>"
    return (start, end)


class ThinkTagSplitter:
    """把流式 TextPartDelta 里的 ``<think>…</think>`` 拆成 (可见文本, 思考增量)。

    标签可能跨 chunk 切开，所以要保留半截前缀。未闭合的思考不进可见文本。
    """

    __slots__ = ("start", "end", "in_think", "hold")

    def __init__(self, start: str = "<think>", end: str = "</think>") -> None:
        start, end = _normalize_thinking_tags((start, end))
        self.start = start
        self.end = end
        self.in_think = False
        self.hold = ""

    def reset(self) -> None:
        self.in_think = False
        self.hold = ""

    def feed(self, piece: str) -> tuple[str, str]:
        if not piece:
            return "", ""
        s = self.hold + piece
        self.hold = ""
        visible: list[str] = []
        thought: list[str] = []
        i = 0
        while i < len(s):
            token = self.end if self.in_think else self.start
            j = s.find(token, i)
            if j < 0:
                cut = _partial_token_suffix(s, i, token)
                chunk = s[i:] if cut < 0 else s[i:cut]
                if self.in_think:
                    thought.append(chunk)
                else:
                    visible.append(chunk)
                if cut >= 0:
                    self.hold = s[cut:]
                break
            chunk = s[i:j]
            if self.in_think:
                thought.append(chunk)
                self.in_think = False
            else:
                visible.append(chunk)
                self.in_think = True
            i = j + len(token)
        return "".join(visible), "".join(thought)

    def flush(self) -> tuple[str, str]:
        leftover = self.hold
        self.hold = ""
        if not leftover:
            return "", ""
        if self.in_think:
            return "", leftover
        # 半截起始标签不是正文（与未闭合思考一致，不进气泡）
        if self.start.startswith(leftover):
            return "", ""
        return leftover, ""


def _partial_token_suffix(s: str, start: int, token: str) -> int:
    """``s[start:]`` 若以 ``token`` 的真前缀结尾，返回截断下标，否则 -1。"""
    tail = s[start:]
    max_k = min(len(tail), len(token) - 1)
    for k in range(max_k, 0, -1):
        if token.startswith(tail[-k:]):
            return len(s) - k
    return -1


def _dedupe_thinking_parts(parts: Sequence[ModelResponsePart]) -> List[ModelResponsePart]:
    """同一响应里相同思考只留一份（先出现的优先，通常是原生 reasoning 字段）。

    MiniMax 等兼容网关会同时给出 ``reasoning_content``（pydantic_ai 已做成
    ThinkingPart）和 ``content`` 里再包一层 ``<think>…</think>``。流式路径下
    标签常不单独成 SSE chunk，补拆后又得到一份相同 ThinkingPart。重复会：
    session log 双记、history 回放双倍 token、thinking_segments 膨胀。
    """
    from pydantic_ai.messages import ThinkingPart

    seen: Set[str] = set()
    kept: List[ModelResponsePart] = []
    for part in parts:
        if isinstance(part, ThinkingPart):
            key = part.content.strip()
            if key and key in seen:
                continue
            if key:
                seen.add(key)
        kept.append(part)
    return kept


def _split_embedded_thinking(
    parts: Sequence[ModelResponsePart],
    thinking_tags: tuple[str, str],
) -> List[ModelResponsePart]:
    """把 TextPart 里以 thinking_tags 包裹的内嵌思考重新拆成独立的 ThinkingPart。

    流式请求下 pydantic_ai 只有在 ``<think>`` 作为独立 SSE chunk 到达时才会拆分思考
    标签（见 _parts_manager.handle_text_delta）；MiniMax 等兼容网关不保证这一点，
    导致 ``<think>...</think>`` 残留在 TextPart 里被原样发往 C 端，且思考内容拿不到
    意图-行为一致性检测。这里按完整文本重新拆分，对齐非流式路径
    （openai 模型的 split_content_into_text_and_thinking）的行为。非 TextPart 与不含
    起始标签的 TextPart 原样透传。

    拆完后按内容去重：网关常把同一段思考同时放进 reasoning 字段和 ``<think>`` 文本。
    """
    from pydantic_ai.messages import TextPart, ThinkingPart

    start_tag, end_tag = _normalize_thinking_tags(thinking_tags)
    result: List[ModelResponsePart] = []
    for part in parts:
        if not isinstance(part, TextPart) or start_tag not in part.content:
            result.append(part)
            continue
        content = part.content
        start_index = content.find(start_tag)
        while start_index >= 0:
            before, content = content[:start_index], content[start_index + len(start_tag) :]
            if before:
                result.append(TextPart(content=before))
            end_index = content.find(end_tag)
            if end_index >= 0:
                think, content = content[:end_index], content[end_index + len(end_tag) :]
                result.append(ThinkingPart(content=think))
            else:
                # 缺少闭合标签：与 ThinkTagSplitter 一致，未闭合内容当思考
                result.append(ThinkingPart(content=content))
                content = ""
            start_index = content.find(start_tag)
        if content:
            result.append(TextPart(content=content))
    return _dedupe_thinking_parts(result)
