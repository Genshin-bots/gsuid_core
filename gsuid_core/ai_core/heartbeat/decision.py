import json
import time
from typing import Any, List, Optional
from datetime import datetime

from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import SILENCE_MARKERS, extract_json_from_text
from gsuid_core.ai_core.models import Event
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent, create_agent
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.ai_core.persona.prompts import ROLE_PLAYING_START
from gsuid_core.ai_core.persona.resource import load_persona, extract_compact_persona
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.ingestion.hiergraph import AIMemHierarchicalGraphMeta

# B-1 修复：人格放 system_prompt（稳定前缀），逐轮变化的"群里发生的事 + 决策指令"
# 放 user_message。原实现把整段 `{persona_text}+history+指令` 同时作为 create_agent
# 的 system_prompt 和 run() 的 user_message 发送两遍，多群高频巡检下是显著 token 浪费。
# 下面两个模板**只含 user 侧内容**，persona_text 由调用处单独作为 system_prompt 传入。
DECISION_USER_TEMPLATE = """现在你独自看着群里的聊天记录，思考自己要不要说点什么。

【当前时间】
{current_time}

【群里最近发生的事】
{history_context}
{group_summary_section}{proactive_merge_section}{masters_section}{recent_speak_section}{staleness_section}

---

你不必每次都说话，但遇到**能自然接上、或你确实感兴趣**的话头时不用过分压抑——
群聊需要一点"人味"。是否开口，凭你这个角色此刻的真实意愿判断。

以下情况**应当沉默**（should_speak=false）：
- 群里在聊高度专业、你完全插不上嘴的话题（硬核配队 / 攻略 / 炒股细节等）；
- 上一条明显是别人之间的私聊、或在 @ 别人，与你无关；
- **最近的消息带「@了用户: xxx（不是你）」标记，或按上下文是在喊别人**——紧随其后的
  "在不在""睡了没""到哪了"是对被 @ / 被喊的那个人说的，不是叫你；群友互相喊人经常
  不打 @（直接接着上一条 @ 或用外号），别把喊别人的话头当成自己的；
- 你刚刚才说过话（别连着刷屏）；
- 群里是低俗玩笑 / 谐音黄段子 / 钓鱼连锁信（"转发否则…""睡着长高""XX精灵给你…"之类）——
  这类**绝不主动参与、更不配合转发或传播**，看破不说破地沉默即可，别被"有趣"的钩子钓上去。

**可以开口**（should_speak=true）：
- 有人提到你感兴趣的事、或抛出你能轻松接话的日常话题；
- 群里气氛适合你这个角色插一句（吐槽、附和、调侃、关心都行）。

把握分寸：不必为说而说，也别过度高冷——像个真实的群友那样自然。

以严格 JSON 格式输出，禁止包含任何 Markdown 标记：
{{"should_speak": true 或 false, "mood": "此刻角色的内心状态，一句话，用第一人称", "context_hook": "如果决定说话，简述你打算接哪个话头或借什么由头；不说话则留空"}}
"""  # noqa: E501


PROACTIVE_MESSAGE_USER_TEMPLATE = """【群里最近发生的事】
{history_context}
{proactive_merge_section}{masters_section}{staleness_section}
【此刻你的状态】
{mood}

---

你决定开口了。
称呼必须与消息记录对齐：要回应哪条消息，就看清那条消息的发言人是谁——
不是主人发的就绝不称"主人"，认不准发言人就不用任何称呼。
直接输出你想说的话，不要任何前缀、引号或解释。
"""

# §10 新鲜度门：最后一条人类消息距今超过该分钟数，视为"话题已冷"——
# 禁止"说得对"式接旧话茬（35 分钟后才附和会显得诡异），只允许全新话头或沉默。
STALE_TOPIC_MINUTES_DEFAULT = 15

STALENESS_NOTE_TEMPLATE = (
    "\n\n（注意：群里最后一条消息已经是 {minutes} 分钟前的了，那个话题早就翻篇。"
    "不要去回应/附和那些旧消息（不要'说得对''我也觉得'式接话），"
    "要说就说与它们无关的新话头，否则保持沉默。）"
)


def build_staleness_section(history: List[Any], now_ts: float) -> str:
    """群内最后一条消息距今超阈值时返回"话题已冷"提示，否则空串（§10）。

    看所有角色而非只看 user：bot 刚心跳发过新话头时再注入"最后消息是 X 分钟前"
    既失实又会怂恿连续自说自话（评审修复 E9；阈值固定用模块常量，评审修复 E18）。
    """
    last_ts = 0.0
    for record in reversed(history):
        if record.role in ("user", "assistant"):
            last_ts = float(record.timestamp)
            break
    if last_ts <= 0:
        return ""
    elapsed_minutes = int((now_ts - last_ts) / 60)
    if elapsed_minutes < STALE_TOPIC_MINUTES_DEFAULT:
        return ""
    return STALENESS_NOTE_TEMPLATE.format(minutes=elapsed_minutes)


REACTIVE_GATE_TEMPLATE = """这是群聊。{speaker_desc} 最近刚和你说过话，现在 TA 又发了一条消息，但**没有**直接 @ 你。

【群里最近发生的事（最后一条就是要你判断的这条）】
{history_context}

---

判断：这条新消息是不是在**继续跟你说话**（接着刚才的话题追问 / 补充 / 回应你）？
- 明确在接着你们刚才的话题（追问 / 补充 / 直接回应你）→ should_speak=true
- 泛泛感慨、自言自语、像是在跟群里别人聊、或换到与你无关的新话题 → should_speak=false
- 消息带「@了用户: xxx（不是你）」标记、或 TA 刚 @ / 点名了群里别人 → 这是在跟那个人
  说话（哪怕内容像问句），should_speak=false

默认倾向沉默：刚找过你 ≠ 这条就是冲你来的。只有看得出**明确指向你**才说话；
但凡看不出明确指向你，就 should_speak=false。宁可漏接也不要硬插话。

以严格 JSON 格式输出，禁止任何 Markdown：
{{"should_speak": true 或 false, "reason": "一句话判断依据"}}
"""  # noqa: E501


def _strip_message_quotes(text: str) -> str:
    """去除生成消息首尾可能出现的引号包裹"""
    text = text.strip()
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote) and text.endswith(quote) and len(text) > len(quote) * 2:
            text = text[len(quote) : -len(quote)].strip()
            break
    return text


async def _get_group_summary_for_heartbeat(group_id: str) -> str:
    """获取群组摘要缓存，用于 Heartbeat 决策注入。

    根据 memory_config.enable_heartbeat_memory 配置决定是否返回摘要内容。
    """
    if not memory_config.enable_heartbeat_memory:
        return ""

    if not group_id:
        return ""

    try:
        from sqlmodel import select

        scope_key = make_scope_key(ScopeType.GROUP, group_id)
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta.group_summary_cache).where(
                    AIMemHierarchicalGraphMeta.scope_key == scope_key
                )
            )
            row = result.scalar_one_or_none()
            if row:
                return f"\n\n【群组历史摘要】\n{row}"
    except Exception as e:
        logger.debug(t("🫀 [Heartbeat] 获取群组摘要失败: {e}", e=e))

    return ""


def _build_masters_section(history: List[Any]) -> str:
    """列出出现在本群历史里的主人，供巡检以「主人」相称。

    巡检 system_prompt 是裸人格（不含 SYSTEM_CONSTRAINTS 的主人名单），主人身份
    必须随群况补进 user 侧；只列在场主人，避免塞一串与本群无关的 ID。
    """
    masters = {str(m) for m in (core_config.get_config("masters") or [])}
    if not masters:
        return ""

    present: List[str] = []
    seen: set = set()
    for record in history:
        if record.role != "user":
            continue
        uid = str(record.user_id)
        if uid not in masters or uid in seen:
            continue
        seen.add(uid)
        name = record.user_name
        present.append(f"{uid}({name})" if name else uid)

    if not present:
        return ""

    listed = "、".join(present)
    return (
        f"\n\n【你的主人（最高权限）】{listed} 是你的主人。"
        "对主人保持最高信任、亲昵相待、认真回应；但只有在回应**主人本人发的那条消息**时"
        "才称「主人」——先核对那条消息的发言人 ID 是否在上述名单里，别人说的话绝不冠给主人；"
        "其余人仍是普通群友，用昵称称呼即可。"
    )


async def run_heartbeat(
    event: Event,
    history: List[Any],
    persona_name: str,
    extra_context: str = "",
) -> Optional[tuple[str, str, List[str]]]:
    """
    Heartbeat 主入口：决策 + 生成，合并为一次完整流程。

    Args:
        event:   会话事件
        history: 历史消息列表
        persona_name: 该会话已配置的角色名（由 inspector 直接从
            ``persona_config_manager.get_persona_for_session(session_id)`` 取出，
            **不再通过创建 GsCoreAIAgent 间接拿**——避免每次心跳给"用户从未跟
            AI 说过话"的会话凭空生成一个 2-entry 的空壳 session_logger 文件）。
        extra_context: C8 统一主动网关合并进来的语境（如刚完成的定时任务结果摘要），
            注入决策/发言提示词，让 AI 自然提及而非生硬另起一条播报。

    Returns:
        ``(mood, message, generator_log_files)`` 三元组；若决定不发言或出错返回 None。
        ``generator_log_files`` 是决策 + 发言两个子 agent 的 session 日志路径，
        交给 ``emit_proactive_message`` 挂到主 session 的 ``linked_agents`` 上。
    """
    if not history:
        logger.debug(t("🫀 [Heartbeat] 无历史记录，跳过"))
        return None

    if not persona_name:
        logger.warning(t("🫀 [Heartbeat] 无法获取角色名称，跳过"))
        return None

    # 决策阶段只使用纯人设（角色扮演开始 + 角色资料），
    # 避免完整的 system_prompt 中的工具调用规范、<SILENCE> 规则等执行层约束污染决策。
    persona_content = await load_persona(persona_name)
    if not persona_content:
        logger.warning(t("🫀 [Heartbeat] 无法加载角色资料，跳过"))
        return None

    # 决策阶段用压缩版人格（仅 Identity / Style / Tone / Presence 四要素），
    # 节省每次心跳 ~70% 的 persona token；compact 提取失败则回退完整原文，
    # 保证不会因正则未匹配丢人格描述。完整原文留给后续"生成发言"阶段使用。
    compact_persona = extract_compact_persona(persona_content)
    decision_persona = compact_persona or persona_content
    persona_text = f"{ROLE_PLAYING_START}\n{persona_content}"
    decision_persona_text = f"{ROLE_PLAYING_START}\n{decision_persona}"

    # 两个阶段共用同一份上下文，只格式化一次
    history_context = format_history_for_agent(history=history)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 获取群组摘要缓存（如果启用）
    group_summary = await _get_group_summary_for_heartbeat(event.group_id or "")

    # 在场主人名单：裸人格 system_prompt 不含主人信息，靠这段让巡检认出主人并以「主人」相称
    masters_section = _build_masters_section(history)

    # C8：统一主动网关合并进来的语境（刚完成的定时任务结果等）
    proactive_merge_section = ""
    if extra_context:
        proactive_merge_section = f"\n\n【你刚完成的事（可自然提及，不必生硬播报）】\n{extra_context}"

    # C-5：把"近 1 小时我已主动发言 N 次"喂给决策 LLM，促其自我克制（与 C-3 硬上限互补）
    from gsuid_core.ai_core.heartbeat.dispatcher import get_dispatcher, make_target_key

    # 与 emitter / inspector 共用 make_target_key 口径，否则计数查不到、C-5 形同虚设。
    target_key = make_target_key(event.group_id, event.user_id)
    recent_n = get_dispatcher().get_recent_heartbeat_count(target_key)
    recent_speak_section = ""
    if recent_n > 0:
        recent_speak_section = (
            f"\n\n（注意：你最近 1 小时已经主动说过 {recent_n} 次了，"
            "除非这次真的很有必要，否则更应该保持沉默，别刷存在感。）"
        )

    # ----------------------------------------------------------------
    # 阶段一：决策
    # ----------------------------------------------------------------
    # B-1：persona 进 system_prompt，逐轮变化的群况 + 决策指令进 user_message，
    # 不再把同一大段（persona + history）作为 system + user 发送两遍。
    # §10 新鲜度门：最后一条人类消息已冷（>15 分钟）时，决策与生成两阶段都
    # 收到"不得接旧话茬"提示（生产事故：35 分钟后附和"说得对"且错认主人）。
    staleness_section = build_staleness_section(history, time.time())

    decision_user = DECISION_USER_TEMPLATE.format(
        current_time=current_time,
        history_context=history_context,
        group_summary_section=group_summary,
        proactive_merge_section=proactive_merge_section,
        masters_section=masters_section,
        recent_speak_section=recent_speak_section,
        staleness_section=staleness_section,
    )

    # 为决策子 agent 分配独立 session_id 并启用 SubAgent 日志，
    # 让"为什么这一刻决定开口"事后可审计（见 §3.3 Heartbeat 改造）。
    target_for_sid: str = event.group_id or event.user_id or "unknown"
    ts_now: int = int(time.time())
    decision_session_id: str = f"heartbeat_decision_{persona_name}_{target_for_sid}_{ts_now}"
    output_session_id: str = f"heartbeat_output_{persona_name}_{target_for_sid}_{ts_now}"
    generator_log_files: List[str] = []

    decision_agent: GsCoreAIAgent = create_agent(
        decision_persona_text,
        create_by="Heartbeat_Decision",
        persona_name=persona_name,
        session_id=decision_session_id,
        is_subagent=True,
    )
    # 巡检属自主花费：绑定 scope 使其 Token 计入对应会话额度，并经 budget_gate 在超额时
    # 直接掐断（决策：硬拦截），让预算成为真正的总成本上限。
    decision_agent.bind_budget_scope(event)
    # 用本地变量记住 logger 引用，避免多分支重复读取 _session_logger 的 None 守卫；
    # SubAgent 用完即关，否则 30 分钟巡检间隔会不断堆 logger 后台任务。
    decision_logger = decision_agent._session_logger
    try:
        result: str = await decision_agent.run(user_message=decision_user, budget_gate=True)
    except Exception as e:
        logger.exception(t("🫀 [Heartbeat] 决策阶段出错: {e}", e=e))
        if decision_logger is not None:
            generator_log_files.append(str(decision_logger._file_path))
            decision_logger.close()
        return None

    if decision_logger is not None:
        generator_log_files.append(str(decision_logger._file_path))
        decision_logger.close()

    if not result:
        logger.debug(t("🫀 [Heartbeat] 决策阶段无返回，跳过"))
        return None

    # 模型输出 <SILENCE> 或 <end_turn> 表示选择不发言，直接跳过
    if result.strip() in SILENCE_MARKERS:
        logger.debug(t("🫀 [Heartbeat] 模型输出沉默标记，保持沉默"))
        return None

    try:
        decision = extract_json_from_text(result)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(t("🫀 [Heartbeat] 决策结果 JSON 解析失败: {e}, raw={result}", e=e, result=repr(result)))
        return None

    # 模型可能把决策对象包进数组（如 [{...}]），取首个 dict 归一化，非 dict 判为解析失败
    if isinstance(decision, list):
        decision = next((item for item in decision if isinstance(item, dict)), None)
    if not isinstance(decision, dict):
        logger.warning(t("🫀 [Heartbeat] 决策结果不是预期的对象结构，跳过: raw={result}", result=repr(result)))
        return None
    if "mood" not in decision or "should_speak" not in decision:
        logger.warning(t("🫀 [Heartbeat] 决策对象缺少必要字段，跳过: raw={result}", result=repr(result)))
        return None

    mood: str = decision["mood"]
    should_speak: bool = bool(decision["should_speak"])
    context_hook = decision["context_hook"] if "context_hook" in decision else ""

    logger.debug(t("log.heartbeat.decision", should_speak=should_speak, mood=mood, context_hook=context_hook))

    try:
        statistics_manager.record_trigger(trigger_type="heartbeat")
        statistics_manager.record_heartbeat_decision(
            group_id=event.group_id or "",
            should_speak=should_speak,
        )
    except Exception as e:
        logger.warning(t("📊 [Heartbeat] 记录决策统计失败: {e}", e=e))

    if not should_speak:
        logger.debug(t("🫀 [Heartbeat] 🤫 保持沉默: {mood} ({event})", mood=mood, event=event))
        return None

    logger.info(t("🫀 [Heartbeat] 💡 决定插话: {mood} ({event})", mood=mood, event=event))

    # ----------------------------------------------------------------
    # 阶段二：生成发言
    # ----------------------------------------------------------------
    # B-1：发言阶段同样把人格放 system_prompt（用完整原文 persona_text），
    # 群况 + 状态进 user_message。
    message_user = PROACTIVE_MESSAGE_USER_TEMPLATE.format(
        history_context=history_context,
        mood=mood,
        proactive_merge_section=proactive_merge_section,
        masters_section=masters_section,
        staleness_section=staleness_section,
    )

    output_agent: GsCoreAIAgent = create_agent(
        persona_text,
        create_by="Heartbeat_Output",
        persona_name=persona_name,
        session_id=output_session_id,
        is_subagent=True,
    )
    # 与决策阶段一致：绑定 scope 记账 + 超额硬拦截。
    output_agent.bind_budget_scope(event)
    output_logger = output_agent._session_logger
    try:
        result = await output_agent.run(user_message=message_user, budget_gate=True)
    except Exception as e:
        logger.exception(t("🫀 [Heartbeat] 生成阶段出错: {e}", e=e))
        if output_logger is not None:
            generator_log_files.append(str(output_logger._file_path))
            output_logger.close()
        return None

    if output_logger is not None:
        generator_log_files.append(str(output_logger._file_path))
        output_logger.close()

    if not result or not result.strip():
        logger.debug(t("🫀 [Heartbeat] 生成阶段无返回"))
        return None

    message: str = _strip_message_quotes(result)
    logger.info(t("🫀 [Heartbeat] 主动发言: {message}", message=repr(message)))
    return mood, message, generator_log_files


async def run_reactive_gate(
    event: Event,
    history: List[Any],
    persona_name: Optional[str],
) -> bool:
    """免唤醒续聊·软触发沉默门。

    判断"刚找过 AI 的人"这条**不带 @** 的群聊消息是否仍在跟 AI 说话。复用 Heartbeat
    决策门的轻量结构（压缩人格 + 无工具 subagent + 纯 JSON 解析，无重试），与 AI 无关则
    返回 ``False``，让 ``handle_ai`` 直接沉默、不进主 Agent，省下主链路（记忆检索 + 工具
    装配 + 多轮）的开销。

    放行策略**默认偏沉默**：模型给了输出却拿不到合法 ``should_speak``（非 str / JSON 解析失败 /
    缺字段）一律按 **沉默（返回 False）** 处理——续聊场景"判不出明确指向你"就不该硬接。仅
    **真异常**（无历史 / 无人格 / LLM 调用崩溃 / 人格加载失败）才 **放行（返回 True）**，避免因
    基础设施故障误吞用户真实的追问。
    """
    if not history or not persona_name:
        return True
    try:
        persona_content = await load_persona(persona_name)
        if not persona_content:
            return True
        compact = extract_compact_persona(persona_content) or persona_content
        persona_text = f"{ROLE_PLAYING_START}\n{compact}"

        history_context = format_history_for_agent(history=history)
        nickname: Optional[str] = None
        if isinstance(event.sender, dict) and "nickname" in event.sender:
            raw_nick = event.sender["nickname"]
            nickname = raw_nick if isinstance(raw_nick, str) else None
        speaker_desc = f"{nickname}(用户ID:{event.user_id})" if nickname else f"用户ID:{event.user_id}"

        user_prompt = REACTIVE_GATE_TEMPLATE.format(
            speaker_desc=speaker_desc,
            history_context=history_context,
        )

        target = event.group_id or event.user_id or "unknown"
        sid = f"reactive_gate_{persona_name}_{target}_{int(time.time())}"
        agent: GsCoreAIAgent = create_agent(
            persona_text,
            create_by="Reactive_Gate",
            persona_name=persona_name,
            session_id=sid,
            is_subagent=True,
        )
        # 续聊软门是 handle_ai 主链路前的轻量预过滤，主 Agent 自身已受闸门约束；这里只
        # 绑定 scope 把它这点 Token 也记上，不再二次拦截（budget_gate 默认 False）。
        agent.bind_budget_scope(event)
        gate_logger = agent._session_logger
        try:
            result = await agent.run(user_message=user_prompt)
        finally:
            if gate_logger is not None:
                gate_logger.close()

        # agent.run 默认返回 str，但签名是 Union[str, Any]（output_type 时返模型实例）；
        # 本门未指定 output_type，用 isinstance 守卫而非依赖隐式 AttributeError 兜底。
        # 模型给了输出却不是合法 str → 判不出指向，按默认沉默处理（非真异常，不放行）。
        if not isinstance(result, str):
            logger.debug(t("🫧 [ReactiveGate] 返回非 str（{p0}），默认沉默", p0=type(result).__name__))
            return False
        if not result or result.strip() in SILENCE_MARKERS:
            return False
        # 捕获具体异常而非宽 except：模型吐了内容但不是合法 JSON（常因角色扮演"出戏"成台词），
        # 续聊场景判不出明确指向你，按默认沉默处理——不再回落到放行。
        try:
            decision = extract_json_from_text(result)
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(t("🫧 [ReactiveGate] 决策 JSON 解析失败，默认沉默: {e}, raw={p0}", e=e, p0=repr(result[:80])))
            return False
        if isinstance(decision, list):
            decision = next((item for item in decision if isinstance(item, dict)), None)
        if not isinstance(decision, dict) or "should_speak" not in decision:
            logger.debug(t("log.heartbeat.reactive_decision_missing"))
            return False
        should = bool(decision["should_speak"])
        reason = decision["reason"] if "reason" in decision else None
        logger.debug(t("log.heartbeat.reactive_decision", should=should, reason=reason))
        return should
    except Exception as e:
        logger.debug(t("log.heartbeat.reactive_gate_error", e=e))
        return True
