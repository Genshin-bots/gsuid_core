"""认知层的类型契约（跨计划冻结接口，路线图 §3.3）。

设计要点：``kinds`` 与 ``scope`` 在 :func:`search_cognition` 里是**必填参数、无内部兜底**。
这不是洁癖——本仓库两个真实 bug（私聊幻影 ``group:{user_id}``、工具路径偷跑 System-2）
的共同根因就是「可选参数被内部兜底成一个看起来合理的值」。需要默认值就在**唯一的配置层**给。
"""

from enum import Enum
from typing import Dict, Tuple, Optional, FrozenSet
from datetime import datetime
from dataclasses import dataclass


class CogKind(str, Enum):
    """可回想对象的语义类型。**不可互相覆盖**——情景 ≠ 事实 ≠ 规范 ≠ 稳定资料。"""

    EPISODE = "episode"
    ENTITY = "entity"
    FACT = "fact"
    PREFERENCE = "preference"
    KNOWLEDGE = "knowledge"
    TOOL_OUTPUT = "tool_output"
    ARTIFACT = "artifact"
    SELF_NOTE = "self_note"
    RECORD = "record"
    IMAGE = "image"
    MEME = "meme"
    MEME_KNOWLEDGE = "meme_knowledge"
    OUTBOUND = "outbound"


# 面向模型的中文标签（进 prompt 的那一份）
KIND_LABEL: Dict[CogKind, str] = {
    CogKind.EPISODE: "片段",
    CogKind.ENTITY: "实体",
    CogKind.FACT: "事实",
    CogKind.PREFERENCE: "偏好·须遵守",
    CogKind.KNOWLEDGE: "知识",
    CogKind.TOOL_OUTPUT: "落盘·可能过时",
    CogKind.ARTIFACT: "任务产物",
    CogKind.SELF_NOTE: "自我笔记",
    CogKind.RECORD: "业务记录",
    CogKind.IMAGE: "图片",
    CogKind.MEME: "表情",
    CogKind.MEME_KNOWLEDGE: "梗知识",
    CogKind.OUTBOUND: "出站",
}

# ⑧ 每轮自动注入的默认切片：与改造前一致（记忆 + 偏好），延迟不回退。
# 全联邦只在工具调用或问答/回指预取时跑。
MEMORY_KINDS: FrozenSet[CogKind] = frozenset({CogKind.EPISODE, CogKind.ENTITY, CogKind.FACT, CogKind.PREFERENCE})
KNOWLEDGE_KINDS: FrozenSet[CogKind] = frozenset({CogKind.KNOWLEDGE})
WORK_KINDS: FrozenSet[CogKind] = frozenset({CogKind.TOOL_OUTPUT, CogKind.ARTIFACT})
MEDIA_KINDS: FrozenSet[CogKind] = frozenset({CogKind.IMAGE, CogKind.MEME})
ALL_KINDS: FrozenSet[CogKind] = frozenset(CogKind)
# 工具未声明 kinds 时的默认面：记忆+知识+落盘。图片/表情/出站/业务记录须显式打开。
DEFAULT_RECALL_KINDS: FrozenSet[CogKind] = MEMORY_KINDS | KNOWLEDGE_KINDS | WORK_KINDS
# query 点名当前说话人 ID：只查身上的记忆（片段+实体+事实+偏好）。
# 不开放知识库/落盘/近窗。近窗只在 DEFAULT_RECALL_KINDS ⊆ kinds 时开，加 EPISODE 不会误开。
SPEAKER_RECALL_KINDS: FrozenSet[CogKind] = frozenset(
    {CogKind.EPISODE, CogKind.ENTITY, CogKind.FACT, CogKind.PREFERENCE}
)


@dataclass(frozen=True)
class CogScope:
    """一次检索的可见范围。**过滤必须下推到各后端**，禁止「先搜全球再内存筛」。"""

    user_id: str
    bot_id: str = ""
    # SELF 记忆用账号 ID，不是平台 ID（onebot 这类会把多账号写成同一个 key）。
    bot_self_id: str = ""
    # 私聊必须是 None。回退成 user_id 只会去查一个空的幻影 group:{user_id}。
    group_id: Optional[str] = None
    # 开发文档库（source=skill_doc）不对普通用户暴露
    include_skill_doc: bool = False
    # 语义性开关，由调用方从配置显式表态（不在这里给默认真值）
    enable_system2: bool = False
    enable_user_global: bool = True
    # LongMem 证据转储（format_eval_memory）。默认关，生产/Agent 评测不得打开。
    memory_eval: bool = False
    # 评测 HTTP clock_at。None=检索用墙上时钟；禁止从用户原文解析。
    clock_at: Optional[datetime] = None

    @property
    def is_private(self) -> bool:
        return self.group_id is None


@dataclass(frozen=True)
class CognitiveHit:
    """一条统一命中。正文仍住在原库里——本对象是索引与关系层，不是第二份正文。"""

    kind: CogKind
    id: str
    title: str
    summary: str
    score: float
    # 数据的时点（``as_of``）：落盘/产物必须带，否则模型会把过期数字当现在
    as_of: str = ""
    # 可 ``read_handle`` 取全文的句柄（to_ / res_ / img_ …）；无则空
    handle: str = ""
    # 分源标注：来源后端名，进 prompt 帮模型判断可信度
    source: str = ""
    # 是否过了相对分下限（只有过门槛的才允许标「高置信」）
    high_confidence: bool = False

    @property
    def label(self) -> str:
        return KIND_LABEL[self.kind]

    def render_line(self, index: int) -> str:
        """单行渲染。空结果只回一行，绝不再拼双段「未找到 + 无匹配 + 长说明」。"""
        # 片段常无 title：旧逻辑只露 40 字，专名被截掉。
        body_budget = 800 if self.kind is CogKind.EPISODE else 160
        head_src = (self.title or self.summary).replace("\n", " ").strip()
        parts = [f"{index}. [{self.label}] {head_src[:body_budget]}"]
        extra = self.summary.replace("\n", " ").strip() if self.summary and self.title else ""
        if extra and extra not in head_src:
            parts.append(extra[:body_budget])
        meta: Tuple[str, ...] = tuple(
            x
            for x in (
                f"as_of={self.as_of}" if self.as_of else "",
                f"读全文: read_handle('{self.handle}')" if self.handle else "",
            )
            if x
        )
        line = "  ".join(parts)
        return f"{line}（{' · '.join(meta)}）" if meta else line
