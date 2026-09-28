"""触发器候选索引：把每条消息的「全量线性扫描」收窄成一次索引查询。

现行 `handle_event` 逐条 `check_command` 扫过所有 SV 的所有触发器。单次匹配已经很便宜
（首字探针 + 预拼 head），贵在次数 —— 892 个触发器实测每条消息 258~543µs，
且全部占用单线程事件循环。

本模块只负责**不漏**：候选集永远是「线性扫描命中集」的超集，
最终判定仍然全部交给 `Trigger.check_command`，本模块不复制任何匹配语义。

按类型分流到各自最合适的索引：

| 类型 | 索引 | 理由 |
|------|------|------|
| `command` / `prefix` / `fullmatch` | 字符 trie | 都是「消息以 prefix+keyword 开头」 |
| `suffix` | 末字分桶 dict | 剥前缀后以 keyword 结尾，末字必然对齐 |
| `keyword` / `regex` / `file` / `message` | 兜底桶 | 语义决定无法安全预判 |

方案与取舍见 `plans/TRIGGER_MATCH_INDEX_20260928.md`。
"""

from __future__ import annotations

from gsuid_core.sv import SL, SV
from gsuid_core.models import Event
from gsuid_core.trigger import Trigger, gap_chars, registry_version

_TRIE_TYPES = frozenset({"command", "prefix", "fullmatch"})


class _TrieNode:
    __slots__ = ("children", "payload", "gap_ok")

    def __init__(self) -> None:
        self.children: dict[str, _TrieNode] = {}
        self.payload: list[Trigger] = []
        # 走到本节点时是否允许先吃掉若干空格再继续匹配子节点
        self.gap_ok: bool = False


class TriggerIndex:
    """由 `SL.lst` 汇总出的候选索引。构造一次，之后每条消息 O(len(msg))。"""

    def __init__(self) -> None:
        self.version: int = registry_version()
        self._gaps: str = gap_chars()
        self._root = _TrieNode()
        self._suffix: dict[str, list[Trigger]] = {}
        self._always: list[Trigger] = []
        self._owner: dict[Trigger, SV] = {}
        for sv in SL.lst.values():
            for bucket in sv.TL.values():
                for trigger in bucket.values():
                    self._owner[trigger] = sv
                    self._place(trigger)

    # ---------- 建索引 ----------

    def _place(self, trigger: Trigger) -> None:
        kind = trigger.type
        gaps = self._gaps
        if kind in _TRIE_TYPES:
            head = trigger.trie_head()
            # head 为空或首字是空白时逐字走查根本进不去，只能挂兜底桶
            if not head or head[0] in gaps:
                self._always.append(trigger)
                return
            self._insert(head, trigger.gap_positions(), trigger)
        elif kind == "suffix":
            tail = trigger.keyword.rstrip(gaps)
            if tail:
                self._suffix.setdefault(tail[-1], []).append(trigger)
            else:
                self._always.append(trigger)
        else:
            self._always.append(trigger)

    def _insert(self, head: str, gap_at: frozenset[int], trigger: Trigger) -> None:
        node = self._root
        for depth, ch in enumerate(head):
            child = node.children.get(ch)
            if child is None:
                child = _TrieNode()
                node.children[ch] = child
            node = child
            if depth + 1 in gap_at:
                node.gap_ok = True
        node.payload.append(trigger)

    # ---------- 查询 ----------

    def candidates(self, ev: Event) -> list[Trigger]:
        """本条消息的候选触发器（命中集的超集）。

        `to_me` 在这里先滤掉：与 `check_command` 首行同义，上提不改变判定结果。
        """
        stripped = ev.raw_text.strip(self._gaps)
        out: list[Trigger] = []
        if stripped:
            self._walk(stripped, out)
            tail = self._suffix.get(stripped[-1])
            if tail is not None:
                out.extend(tail)
        out.extend(self._always)
        if ev.is_tome:
            return out
        return [t for t in out if not t.to_me]

    def _walk(self, text: str, out: list[Trigger]) -> None:
        """沿 trie 走一遍，收集沿途所有节点的 payload。

        同一节点可能被两条路径先后到达（跳过空格与否），重复收候选无害：
        下游 `valid_event` 是按 Trigger 去重的 dict。
        """
        gaps = self._gaps
        text_len = len(text)
        stack: list[tuple[_TrieNode, int]] = [(self._root, 0)]
        while stack:
            node, pos = stack.pop()
            if node.payload:
                out.extend(node.payload)
            if pos >= text_len:
                continue
            child = node.children.get(text[pos])
            if child is not None:
                stack.append((child, pos + 1))
            if node.gap_ok and text[pos] in gaps:
                nxt = pos
                while nxt < text_len and text[nxt] in gaps:
                    nxt += 1
                if nxt < text_len:
                    after = node.children.get(text[nxt])
                    if after is not None:
                        stack.append((after, nxt + 1))

    def owner_of(self, trigger: Trigger) -> SV | None:
        return self._owner.get(trigger)


_INDEX: TriggerIndex | None = None


def get_trigger_index() -> TriggerIndex:
    """取全局索引。触发器集合有变动（注册了新触发器）时自动重建。"""
    global _INDEX
    index = _INDEX
    if index is None or index.version != registry_version():
        index = TriggerIndex()
        _INDEX = index
    return index


def reset_trigger_index() -> None:
    """丢弃缓存，下次访问时重建。测试与插件热重载用。"""
    global _INDEX
    _INDEX = None
