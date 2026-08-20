"""套件协议 + 槽位表 + ``CONTEXT_BLOCK_ORDER``（跨计划冻结接口）。

块名表是**唯一**的装配顺序定义（路线图 §3.1）。顺序以历史 ``assemble_dynamic_context``
的实际 append 顺序为准（那份 docstring 曾写错成「历史→情绪→…」）。
各套件只填命名块，不得自己拼接顺序——否则第 12 个块会插到身份锚前面。
"""

from typing import Tuple, Mapping, Callable, Optional, Awaitable, FrozenSet
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# 装配顺序单源。identity / history 归内核填（前者是密封身份锚，后者是消息基础设施）。
# group_context 在 history 之后、memory 之前（群词汇映射移出 system，见 2C）。
# plan_hint 在 task 之后（袖珍规划前置，见 6D）。
CONTEXT_BLOCK_ORDER: Tuple[str, ...] = (
    "mood",
    "relationship",
    "voice_anchor",
    "identity",
    "history",
    "group_context",
    "memory",
    "task",
    "plan_hint",
    "chitchat_style",
    "transaction_priority",
    "report_titles",
    "soft_trigger",
    "plugin_hints",
)

# 单轮动态块合计目标 ≤2000 字：join 时按块截断并 warning。
BLOCK_CHAR_BUDGET: Mapping[str, int] = {
    "mood": 80,
    "relationship": 100,
    "voice_anchor": 100,
    "identity": 80,
    "history": 600,
    "group_context": 200,
    "memory": 800,
    "task": 250,
    "plan_hint": 250,
    "chitchat_style": 160,
    "transaction_priority": 80,
    "report_titles": 80,
    "soft_trigger": 220,
    "plugin_hints": 150,
}

# 建 session 时才允许写的稳定块（H29）。运行中禁止改 system。
STABLE_BLOCK_NAMES: FrozenSet[str] = frozenset({"self_model", "group_profile"})

_KNOWN_BLOCKS: FrozenSet[str] = frozenset(CONTEXT_BLOCK_ORDER) | STABLE_BLOCK_NAMES
# 口吻 / 口气 / 身份是同一组角色提示，拼在一起中间不要空行。
_CUE_BLOCK_CLUSTER: FrozenSet[str] = frozenset({"voice_anchor", "identity"})


def is_known_block(name: str) -> bool:
    """块名白名单校验。未知名一律拒绝，防套件私自插块。"""
    return name in _KNOWN_BLOCKS


def _apply_block_budget(name: str, text: str) -> str:
    """超 per-block 预算则截断。预算表缺名时不截（稳定块不在此表）。"""
    if name not in BLOCK_CHAR_BUDGET:
        return text
    budget = BLOCK_CHAR_BUDGET[name]
    if len(text) <= budget:
        return text
    logger.warning(t("log.agent.context_block_truncated", name=name, before=len(text), after=budget))
    return text[: max(0, budget - 1)] + "…"


def join_named_blocks(blocks: Mapping[str, str]) -> str:
    """按 ``CONTEXT_BLOCK_ORDER`` 拼装；口吻/口气/身份连成一段，其余块仍 ``\\n\\n``。"""
    pieces: list[str] = []
    cues: list[str] = []
    for name in CONTEXT_BLOCK_ORDER:
        if name not in blocks:
            continue
        text = blocks[name]
        if not text:
            continue
        text = _apply_block_budget(name, text)
        if name in _CUE_BLOCK_CLUSTER:
            cues.append(text)
            continue
        if cues:
            pieces.append("".join(cues))
            cues = []
        pieces.append(text)
    if cues:
        pieces.append("".join(cues))
    return "\n\n".join(pieces)


# ── 槽位表 ──


@dataclass(frozen=True)
class KitSlot:
    """一个可替换单位。``exclusive=False`` 表示允许多套件同时挂（观察扇出）。"""

    name: str
    default_kit_id: str
    exclusive: bool
    sealed: bool
    description: str


def _slot(name: str, default: str, desc: str, *, exclusive: bool = True, sealed: bool = False) -> KitSlot:
    return KitSlot(name, default, exclusive, sealed, desc)


# 槽名不含点号：点号既是槽名一部分又是配置层级分隔符会有解析歧义（补正 C-7）。
KIT_SLOTS: Tuple[KitSlot, ...] = (
    # 记忆的入站观察随 memory 槽走（关 memory 就该不观察），故本槽默认只有表情
    _slot("inbound_observe", "gscore.meme", "入站旁路观察（表情入库；可多占）", exclusive=False),
    _slot("memory", "gscore.memory", "检索 + 注入 + 工具轨迹 + 记忆工具"),
    _slot("favorability", "gscore.favorability", "关系温度：读 / 注入 / 结算 / 衰减"),
    _slot("mood", "gscore.mood", "人格情绪：注入 + 收尾更新"),
    _slot("self_cognition", "gscore.self_cognition", "稳定前缀自述 + 每轮关系行"),
    _slot("group_profile", "gscore.group_profile", "稳定前缀群画像"),
    _slot("planning_context", "gscore.planning_context", "长任务文案 + has_actionable"),
    _slot("classifier", "gscore.classifier", "意图分类"),
    _slot("reactive_gate", "gscore.reactive_gate", "软触发沉默门"),
    _slot("scaffold", "gscore.scaffold", "TurnGraph 消费 / CheapGate / 脚手架 hints"),
    _slot("session_mute", "gscore.session_mute", "会话静默窗"),
    _slot("statistics", "gscore.statistics", "统计记账（只上报，不扣减）"),
    _slot(
        "decision_distill",
        "gscore.decision_distill",
        "thinking 决策蒸馏（H08，可关）",
        exclusive=False,
    ),
    _slot("tool_assembly", "gscore.tool_assembly", "五层工具装配 + find_tools"),
    _slot("fileos", "gscore.fileos", "长回执落盘折叠"),
    _slot("post_tool", "gscore.post_tool", "工具后契约注入"),
    _slot("quality", "gscore.quality", "假完成 / 出图纠正"),
    _slot("speech", "gscore.speech", "出站话术态（密封：可关不可替）", sealed=True),
    _slot("persona_identity", "gscore.identity", "身份锚（密封：不可关）", sealed=True),
)

SLOTS_BY_NAME = {s.name: s for s in KIT_SLOTS}

OFF = "off"


def slot_of(name: str) -> KitSlot:
    return SLOTS_BY_NAME[name]


def is_known_slot(name: str) -> bool:
    return name in SLOTS_BY_NAME


# ── 套件协议 ──


@dataclass
class AgentKit:
    """一个套件 = 一组 hook 注册 + 可选 init_step + 拥有的工具名。

    ``register`` 只做「在哪些点位调用哪些实现」；实现代码留在原模块
    （``ai_core/memory/`` 等），套件不重写记忆系统。
    """

    kit_id: str
    slot: str
    display_name: str
    owns_tools: Tuple[str, ...] = ()
    sealed: bool = False
    enabled_by_default: bool = True
    init_step: Optional[Callable[[], Awaitable[None]]] = None

    def register(self) -> None:
        """挂 hook。子类必须实现。"""
        raise NotImplementedError

    def unregister(self) -> None:
        """摘本 kit_id 全部 hook，并卸掉 ``owns_tools``（防空壳工具留在 schema 里）。"""
        from gsuid_core.ai_core.hooks import drop_hooks_for_kit
        from gsuid_core.ai_core.register import unregister_tool

        drop_hooks_for_kit(self.kit_id)
        for name in self.owns_tools:
            unregister_tool(name)
