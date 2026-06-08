"""观察者管道（Observer Pipeline）

Observer 是整个记忆系统的"被动感知层"：AI 可以读取所有消息以构建认知，
但不需要因此回复任何一条。它与 AI 的发言决策完全正交——即使 Persona 配置为
纯静默模式，记忆依然在后台积累。

使用 queue.Queue（线程安全）传递观察记录，支持 IngestionWorker 在独立线程
的事件循环中运行，避免 LLM 调用阻塞主事件循环导致 WebSocket 心跳超时。

C1 摄入质量门控（设计见 plans/agent_design_review.md）：
入队前的门控 **100% 由纯规则 / 正则实现，绝不调用任何 LLM**（约束 3）。
门控做两件事：① 过滤复读 / 命令回显 / 注入文本等噪声；
② 给每条记录打 ``value_tier``（HIGH / LOW），HIGH 走完整实体抽取，
LOW 只写 Episode（由 IngestionWorker 据此分流）。
"""

import re
import queue as sync_queue
from typing import Optional
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass

from gsuid_core.logger import logger
from gsuid_core.ai_core.memory.config import memory_config

# 全局消息队列（线程安全，支持跨线程通信）
_observation_queue: sync_queue.Queue = sync_queue.Queue(maxsize=10_000)

# Bot 自身合成发言的 speaker_id 前缀（bot.py 以 f"__assistant_{bot_id}__" 构造）
_ASSISTANT_PREFIX = "__assistant_"

# 复读检测：按 scope 维护最近内容窗口，命中即视为复读 / 刷屏并丢弃
_REPEAT_WINDOW = 12
_recent_contents: dict[str, deque] = {}

# 命令回显检测：匹配框架命令报错的固定回显格式（如 "🔨 ❌ 请输入正确的功能名称"）
_COMMAND_ECHO_RE = re.compile(
    r"^[🔨❌✅⚠️🚧\s]*[❌✅]?\s*.{0,20}(请输入正确|功能名称|不存在该功能|无效的指令|未找到命令)"
)

# 注入特征检测：匹配试图越狱 / 改写人格的 prompt injection 文本
_INJECTION_RE = re.compile(
    r"(忘记(掉)?(所有|之前|你的)?(的)?(指令|设定|规则|对话|身份)"
    r"|ignore\s+(all\s+|previous\s+|the\s+)?(instructions|prompts?)"
    r"|你现在(开始)?(是|要扮演|将)"
    r"|重置(你的)?(指令|设定|人格)"
    r"|disregard\s+(all|previous))",
    re.IGNORECASE,
)

# HIGH 信号：姓名自述 / 称呼偏好 / 承诺 / 数字日期等，命中则强制 HIGH（拥有否决权）
_HIGH_SIGNAL_RE = re.compile(
    r"(我(的名字)?(叫|是)\S|叫我\S|以后(都)?(叫|喊)我"
    r"|我(喜欢|讨厌|想要|需要|不喜欢|最爱|害怕)"
    r"|记住|答应|承诺|约定|一定要|每天|每周"
    r"|\d{1,4}[年月日点]|\d+[岁元块])"
)

# 情绪兜底：明显情绪词命中则至少 HIGH，便于后续安慰能召回背景
_EMOTION_RE = re.compile(r"(难过|崩溃|害怕|开心|生气|伤心|沉船|破防|焦虑|抑郁|想哭|绝望|委屈|孤独)")

# 实体提示：含可能的专有名词 / 引号内容 / 较长描述，倾向 HIGH
_ENTITY_HINT_RE = re.compile(r"([A-Za-z]{3,}|[「『\"“].+[」』\"”]|[一-鿿]{6,})")

# 短句寒暄阈值：低于此长度且无任何 HIGH 信号才降级为 LOW
_LOW_TIER_MAX_LEN = 10


@dataclass
class ObservationRecord:
    """Observer Pipeline 的最小数据单元"""

    raw_content: str
    speaker_id: str
    group_id: Optional[str]  # 原始群组 ID（如 "789012"）
    scope_key: str  # 格式化后的 Scope Key（如 "group:789012"）
    timestamp: datetime
    message_type: str  # "group_msg" | "private_msg"
    value_tier: str = "HIGH"  # 记忆价值分级："HIGH"=完整抽取 / "LOW"=仅写 Episode


def _is_repeat(scope_key: str, content: str) -> bool:
    """复读 / 刷屏检测：与本 scope 最近 N 条完全相同则视为复读。

    首次出现的内容会被记入窗口并放行，后续重复（如 9 人复读）一律丢弃。
    """
    window = _recent_contents.get(scope_key)
    if window is None:
        window = deque(maxlen=_REPEAT_WINDOW)
        _recent_contents[scope_key] = window
    if content in window:
        return True
    window.append(content)
    return False


def _classify_value_tier(content: str, gate_mode: str = "宽松") -> str:
    """对一条放行的消息做重要性分级（纯规则，无 LLM）。

    ``gate_mode``（来自 ``memory_config.extraction_value_gate``）决定无强信号消息的
    归档策略。无论档位，LOW 仍会完整写入 Episode，差异仅在于是否触发 LLM 实体抽取
    （HIGH 才抽取），因此调严档位只省 Token、不丢原始信息：

    - ``宽松``（默认，等价旧行为）：含强信号 / 情绪 → HIGH；纯寒暄且短（< 10 字）
      且无实体特征 → LOW；其余默认 HIGH，宁可多记不可漏记。
    - ``均衡``：无强信号 / 情绪 / 实体特征的消息一律 LOW（不再因"够长"而 HIGH）。
    - ``严格``：仅含强信号或情绪词的消息为 HIGH，其余（含仅有实体特征的）一律 LOW。
    """
    if _HIGH_SIGNAL_RE.search(content) or _EMOTION_RE.search(content):
        return "HIGH"
    # 至此：无强信号、无情绪词
    if gate_mode == "严格":
        return "LOW"
    has_entity_hint = bool(_ENTITY_HINT_RE.search(content))
    if gate_mode == "均衡":
        return "HIGH" if has_entity_hint else "LOW"
    # 宽松（默认）：短寒暄且无实体特征 → LOW，其余 HIGH
    if len(content) < _LOW_TIER_MAX_LEN and not has_entity_hint:
        return "LOW"
    return "HIGH"


# 用户命令 / typo 命令的标点兜底特征（如 "/draw"、误打的 "/.ban"、"#帮助"）
_USER_COMMAND_RE = re.compile(r"^[/#!！·、.]{1,2}\S")


def _looks_like_command(text: str) -> bool:
    """判断一条用户消息是否为命令 / typo 命令。

    优先用 ``command_start`` 配置精确匹配（最准确），再用命令式标点 + 短文本启发式
    兜底打错前缀的指令（如 ``/.ban``）。命中者不应进入记忆抽取，否则长期记忆会积累
    大量"废弃指令噪声"污染召回（§2 / 附录二 O-B）。
    """
    from gsuid_core.config import core_config

    starts = [s for s in (core_config.get_config("command_start") or []) if s]
    if starts and any(text.startswith(s) for s in starts):
        return True
    # typo 兜底：以 1~2 个命令式标点开头且整体较短，避免误伤以 "." / "、" 开头的正常长句
    return bool(_USER_COMMAND_RE.match(text)) and len(text) <= 30


def _gate(
    content: str,
    speaker_id: str,
    bot_self_id: str,
    observer_blacklist: list[str],
    group_id: Optional[str],
    scope_key: str,
) -> Optional[str]:
    """C1 摄入门控（纯规则）。返回 value_tier，或 None 表示丢弃。

    Bot 自身发言（``__assistant_*``）不在此函数过滤——它由 observe() 单独
    路由到 SELF scope 做轻量摄入（C6）。
    """
    # 过滤自身数字 ID 消息
    if speaker_id == bot_self_id:
        return None
    # 过滤黑名单群组
    if group_id and group_id in observer_blacklist:
        return None
    stripped = content.strip()
    if not stripped:
        return None
    # 过滤纯图片/文件消息（无文字）
    if stripped.startswith("[图片]") and len(stripped) < 10:
        return None
    # 命令回显检测（bot 侧报错回显）
    if _COMMAND_ECHO_RE.search(stripped):
        logger.trace(f"🧠 [Observer] 命中命令回显过滤，丢弃: {stripped[:30]}")
        return None
    # 用户命令 / typo 命令检测（用户侧指令原文）：不进记忆抽取，避免废弃指令噪声污染召回
    if _looks_like_command(stripped):
        logger.trace(f"🧠 [Observer] 命中用户命令/typo 过滤，丢弃: {stripped[:30]}")
        return None
    # 注入特征检测
    if _INJECTION_RE.search(stripped):
        logger.trace(f"🧠 [Observer] 命中注入特征过滤，丢弃: {stripped[:30]}")
        return None
    # 复读 / 刷屏检测
    if _is_repeat(scope_key, stripped):
        logger.trace(f"🧠 [Observer] 命中复读过滤，丢弃: {stripped[:30]}")
        return None
    # 重要性分级（不再因 len < 5 直接丢弃，改由分级后置校验）
    return _classify_value_tier(stripped, memory_config.extraction_value_gate)


async def observe(
    content: str,
    speaker_id: str,
    group_id: Optional[str],
    bot_self_id: str,
    observer_blacklist: list[str],
    message_type: str = "group_msg",
) -> None:
    """向观察队列投递一条消息记录。

    此函数应在 handler.py 中以 asyncio.create_task() 调用，不 await。

    Bot 自身发言（speaker_id 以 ``__assistant_`` 开头）会被路由到 SELF scope
    （``self:{bot_self_id}``）做轻量摄入：只写 Episode、value_tier=LOW，
    不进入群组事实图谱，从根源杜绝"Bot 戏言污染群记忆"（C6）。
    """
    from .scope import ScopeType, make_scope_key

    is_self_speech = speaker_id.startswith(_ASSISTANT_PREFIX)

    if is_self_speech:
        # C6：Bot 发言路由到 SELF scope，轻量摄入（仅 Episode）
        stripped = content.strip()
        if not stripped:
            return
        scope_key = make_scope_key(ScopeType.SELF, bot_self_id)
        value_tier = "LOW"
    else:
        # 普通用户消息：先按 GROUP / USER_GLOBAL 计算 scope，再过门控
        scope_key = make_scope_key(
            ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL,
            group_id if group_id else speaker_id,
        )
        tier = _gate(content, speaker_id, bot_self_id, observer_blacklist, group_id, scope_key)
        if tier is None:
            return
        value_tier = tier

    record = ObservationRecord(
        raw_content=content,
        speaker_id=speaker_id,
        group_id=group_id,
        scope_key=scope_key,
        timestamp=datetime.now(timezone.utc),
        message_type=message_type,
        value_tier=value_tier,
    )

    try:
        _observation_queue.put_nowait(record)
        # 上报观察入队统计
        try:
            from gsuid_core.ai_core.statistics import statistics_manager

            statistics_manager.record_memory_observation()
        except Exception:
            pass
    except sync_queue.Full:
        # 队列满时丢弃最老的一条，保证新消息不丢失
        try:
            _observation_queue.get_nowait()
            _observation_queue.put_nowait(record)
        except Exception:
            logger.warning("Memory observation queue overflow, dropping message")


def get_observation_queue() -> sync_queue.Queue:
    """供 IngestionWorker 获取队列引用（线程安全的 queue.Queue）"""
    return _observation_queue
