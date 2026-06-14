"""知识库长文分片（chunking）

把数十万字的长文切成适配嵌入模型的小片，每片单独成向量。这是"批量导入长文"的前置：
默认本地模型 ``bge-small-zh-v1.5`` 仅 512 token 上限，整段长文嵌入会被**静默截断**，
绝大部分内容不进向量、永不可检索（详见 plans/knowledge_base_bulk_import_assessment_20260614.md §3.1）。

策略：优先按 段落 → 句子 切，超长句再定长+重叠兜底，**不无脑硬切**，避免把一个完整事实
从中间切断导致两片都召回不到。相邻片保留少量重叠以保完整语义。
"""

import re
from typing import List

# 句末标点（中英文）：在这些位置允许断句
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;\n])")

# 片长 / 重叠的安全边界，防越界配置把单片撑爆或重叠吞掉全部进度
DEFAULT_CHUNK_SIZE = 400
DEFAULT_CHUNK_OVERLAP = 60
MIN_CHUNK_SIZE = 50
MAX_CHUNK_SIZE = 4000


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _split_long_sentence(s: str, max_chars: int, overlap: int) -> List[str]:
    """对超过 max_chars 的单个句子做定长+重叠切分。"""
    step = max(max_chars - overlap, 1)
    return [s[i : i + max_chars] for i in range(0, len(s), step)]


def split_text(
    text: str,
    max_chars: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[str]:
    """把长文切成 ``<= max_chars`` 的片列表。

    Args:
        text: 原始长文
        max_chars: 单片最大字符数（会被夹到 [MIN_CHUNK_SIZE, MAX_CHUNK_SIZE]）
        overlap: 相邻片重叠字符数（会被夹到 [0, max_chars//2]）

    Returns:
        分片后的文本列表（已去除空片）；输入为空时返回 []。
    """
    text = (text or "").strip()
    if not text:
        return []

    max_chars = _clamp(int(max_chars), MIN_CHUNK_SIZE, MAX_CHUNK_SIZE)
    overlap = _clamp(int(overlap), 0, max_chars // 2)

    # 1. 先按空行分段
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    chunks: List[str] = []
    buf = ""

    def _flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for p in paras:
        # 段落能并入当前缓冲且不超限 → 并入
        if buf and len(buf) + len(p) + 1 <= max_chars:
            buf = f"{buf}\n{p}"
            continue
        # 当前缓冲已满，先落盘
        _flush()
        if len(p) <= max_chars:
            buf = p
            continue
        # 2. 段落本身超长：按句子切
        for s in _SENT_SPLIT_RE.split(p):
            s = s.strip()
            if not s:
                continue
            if len(buf) + len(s) + 1 <= max_chars:
                buf = f"{buf}\n{s}" if buf else s
                continue
            _flush()
            if len(s) <= max_chars:
                buf = s
            else:
                # 3. 单句仍超长：定长+重叠兜底
                pieces = _split_long_sentence(s, max_chars, overlap)
                chunks.extend(pieces[:-1])
                buf = pieces[-1] if pieces else ""
    _flush()

    # 4. 重叠：相邻片之间拼接上一片尾部 overlap 个字符（首片不变），增强跨片语义连续性。
    #    仅当 overlap>0 且能切出多片时生效。
    if overlap > 0 and len(chunks) > 1:
        with_overlap: List[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            merged = f"{tail}{chunks[i]}"
            # 拼接后若超限则不加重叠，保证单片不越过 max_chars 触发模型截断
            with_overlap.append(merged if len(merged) <= max_chars else chunks[i])
        chunks = with_overlap

    return chunks
