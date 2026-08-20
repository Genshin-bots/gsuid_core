"""主人格出站话术策略：单表面 + 等待/追问/回灌分流。

与业务域词表无关；用 turn 来源、交付态、句式结构判定。

期望阶段（长信息任务）：
1 接任务可短应 → 2 检索/委派决策不发言 → 3 委派前一句「会比较久」
→ 4/5/6 子代理静默 → 7 发图后一句收尾。
"""

from __future__ import annotations

import re
from typing import Literal, Sequence

from gsuid_core.ai_core.utils import is_silence_marker
from gsuid_core.ai_core.content_guard import is_observation_untrusted
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    is_timeless_aggregate as _is_timeless_aggregate,
    fact_pack_is_multi_point as _fact_lines_multi_point,
)

SpeechPolicy = Literal[
    "free",
    "silence_only",
    "status_ok",
    "framework_nudge",
    "framework_deliver",
    "delivered",
]

# 单轮主通道可见台词上限（兜底；DELIVERED 终局态落地后主要是防多 TextPart 刷屏）
MAIN_CHANNEL_VISIBLE_LIMIT = 3

# 用户追问进度：疑问/催促闭类（非业务域）
_STATUS_INQUIRY_RE = re.compile(
    r"(好了吗|好了没|弄好了吗|弄好没|完成了吗|画好了吗|出好了吗|"
    r"还好了没|怎么样了|咋样了|如何了|进度|还要多久|要多久|"
    r"好了\s*[？?]?$|弄好了\s*[？?]?$|图呢|结果呢|好了没啊)",
    re.IGNORECASE,
)
# 有活跃任务时的极短催促/省略
_STATUS_SHORT_NUDGE_RE = re.compile(r"^[\s…·.。!！?？嗯啊呢哈]+$|^(呢|啊|？|\?|嗯\??|然后呢|后来呢)$")

# 完成态交付口吻（未真正发图却宣称已交付）
_PREMATURE_DELIVERY_RE = re.compile(
    r"(出了个?图|画好了|弄好了|做好了|生成好了|渲染好了|"
    r"图好了|已经画|已经弄好|已经出图|详情让.{0,12}出了|"
    r"翻完了|整理好了|弄完了|查完了)",
    re.IGNORECASE,
)

# 空交付 / 摆烂：声称**已有材料**却不出图、推给用户「再喊我」（须配合 fact_pack_pending）
_EMPTY_HANDOFF_RE = re.compile(
    r"(念不(动|完|下)|懒得念|太长了.{0,10}(不|懒|念)|"
    r"记着呢|全(都)?(记|在)(着|里|呢)|细节(全)?在|"
    r"要哪段|再喊我|有点印象|"
    r"全记着|都在里面了(?!…?图)|"
    r"先睡了.{0,8}$)",
    re.IGNORECASE,
)

# 过程/框架元话语：对用户可见即 OOC（结构通道词，非业务域）
_PROCESS_META_RE = re.compile(
    r"(时效存疑|自己再验|数据没刷|没刷出来|没法.{0,8}编数字|"
    r"回炉了?你再|回炉|系统校验|框架[·・.]任务|产物句柄|"
    r"long_structured|tool_return|inline_head|"
    r"how_to_read|persisted\s+id)",
    re.IGNORECASE,
)

# 交付状态汇报：模型以系统日志口吻向用户播报「任务/发送已完成、无需再说话」。
# 双信号共现才命中（精度优先）：
#   A 交付/完成播报 —— (任务|图|消息|产物|结果) × 完成态发送动词；
#   B 行政化收束 —— 「无需/不必 + 追加/补充/发言/回复」的自我静默判定。
# 只说「已经发给你啦」（面向用户的自然交付）不命中 B，放行。
_DELIVERY_REPORT_RE = re.compile(
    r"(任务|图(片)?|消息|产物|结果|数据)[^。！？\n]{0,8}(已完成|完成|已发送|已发给|已交付|已发出|发送完毕|已经发送|已经完成)"
    r"|(已|已经)?(发送给|发给|发到)\s*[^\s，。！？]{1,12}",
    re.IGNORECASE,
)
_DELIVERY_META_CLOSE_RE = re.compile(
    r"(无需|不必|不用|无须)[^。！？\n]{0,6}(追加|补充|再?发言|再?回复|多说|多言|赘述)",
    re.IGNORECASE,
)


def looks_like_delivery_status_narration(text: str) -> bool:
    """是否交付状态汇报（系统日志腔对用户播报，OOC）。

    判据 = 交付/完成播报 × 行政化自我静默 同段共现；单一信号不命中，
    保护「已经发给你啦」这类面向用户的自然交付句。
    """
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if _DELIVERY_META_CLOSE_RE.search(body) is None:
        return False
    return _DELIVERY_REPORT_RE.search(body) is not None


# 在途一句角色短句额度。不定死话术；过程动词/清单仍拦。
IN_FLIGHT_SPEECH_MAX = 40
# 仍算等待句（测试/旧会话），但不是唯一合法出口。
IN_FLIGHT_WAIT_TEMPLATES: tuple[str, ...] = ("马上好。", "嗯，在弄了。")
_INFLIGHT_PROCESS_VERB_RE = re.compile(r"(还在(渲|画|跑|查)|在渲|查中|还没(画|渲|写)好)")

# 等待安慰 / 委派前「会比较久」声明（步骤 3）
_WAIT_COMFORT_RE = re.compile(
    r"(等(一?下|会|会儿)|稍等|先等|等我|慢点|"
    r"(画|翻|弄|查|整).{0,6}(一下|会儿)|"
    r"先(翻|画|弄)|马上|很快|"
    r"(比较|有点|会)?(久|慢|费时|花(点|些)?时间)|"
    r"耐心|等着|先等着|得翻|得查|得弄|翻会儿|查会儿|"
    r"别急|慢慢|稍后|等等我)",
    re.IGNORECASE,
)

# 编排元话语：叙述内部 worker（结构=让 X 去/做 + 拉丁标识）
_ORCHESTRATION_NARRATION_RE = re.compile(
    r"(让|叫|派)\s*[A-Za-z][A-Za-z0-9_]{2,}\s*(去|来|出|画|渲|跑|执行|处理)|"
    r"\b(render_agent|research_agent|create_subagent|agent_profile)\b|"
    r"\brender\b.{0,6}(出|画|渲)|"
    r"(出|画|渲).{0,6}\brender\b",
    re.IGNORECASE,
)

# 引导性收尾（助理腔）：主动邀约用户继续/再查——角色应表达完即停
_OPEN_SOLICIT_RE = re.compile(
    r"(要不要我|要不要|需不需要|还要我|要我再|要我继续|要我换|要我帮|"
    r"要不要继续|还想知道|还有(什么|哪)|有什么想|需要我|"
    r"我再(帮|查|搜|找)|我可以再)",
    re.IGNORECASE,
)

# 主人格台词里的「长结构体」：多标题/多段/表 → 应走 render 而非刷屏
_MD_HEADING_RE = re.compile(r"(?m)^#{1,3}\s+\S|^\*{0,2}\*\*[^*\n]{2,48}\*\*")

STATUS_INQUIRY_HINT = (
    "\n\n（进行中任务·用户在问进度：先 list_my_kanban_tasks / artifact_get_recent 查状态；"
    "再以「我」的口吻短句告知还在弄/弄好了/翻砸了。"
    "禁止空口编进度；禁止念节点名/任务号/句柄/代理；"
    "已有图则 send_message_by_ai 发出再说话。）"
)

_WALL_CLOCK_CLOSE = (
    "（系统提示：本轮处理耗时已超预算。立即基于已有信息用角色口吻给出最终回复；"
    "除非是为已有事实包委派 render_agent 出图，否则不要再发起新的工具调用；"
    "信息不全就如实说明现状，绝不编造。"
    "禁止对用户念内部节点名或编排流程；禁止用多段标题/列表把长信息念成台词。"
    "若仍在等待后台：角色口吻短句或 <SILENCE>，禁止过程叙事与任务编号。）"
)

_WALL_CLOCK_PIPELINE = (
    "（系统提示：本轮处理耗时已超预算，但**已有事实包未出图**——"
    "这是硬例外，**禁止**因预算停工具。"
    "你必须立刻 "
    'create_subagent(agent_profile="render_agent", task=本轮事实包或 res_ 句柄) 出图；'
    "禁止新开检索；禁止长文当台词；禁止说「查完了/资料里有/念不动」却不出图；"
    "可先一句「等一下…」再委派；出图完成前其余 <SILENCE>；禁止念内部节点名。）"
)

_RENDER_DELEGATE_NUDGE = (
    "（系统校验·内部轮：不对用户闲聊）"
    "本轮已有检索/工具材料或长结构内容且未出图 → **必须** "
    'create_subagent(agent_profile="render_agent", task=把本轮要点做成信息图)；'
    "禁止再输出长正文/标题列表；禁止引导性追问。"
    "仅当本轮工具全部失败且无任何可展示要点 → 只输出 <SILENCE>。"
    "禁止向用户解释本校验、禁止抱怨、禁止念节点名。"
)

# 主人格把长结构念成台词时的纠正（比通用 render nudge 更硬）
_REPORT_SPEECH_NUDGE = (
    "（系统校验·内部轮）你刚才用多段标题/列表把长信息念成了台词，这不允许。"
    '立即 create_subagent(agent_profile="render_agent", '
    "task=将本轮已查到的要点做成一张信息图)；"
    "本轮对用户只可 <SILENCE> 或发图后一句角色短句；禁止再念表、禁止要不要再查。"
)

_STATUS_ZERO_TOOL_NUDGE = (
    "（系统校验：用户在追问进行中事项的进度，但你本轮未调用任何查询工具就报了状态。"
    "立即 list_my_kanban_tasks 或 artifact_get_recent 核实；"
    "再以角色短句说明，禁止空口「快好了」。）"
)


def spoken_user_body(text: str) -> str:
    """剥装配外壳后的真人句。通道粘连的 ASCII 唤醒词不是话题。"""
    body = (text or "").strip()
    if not body:
        return ""
    had_envelope = "--- 消息 ---" in body
    if had_envelope:
        body = body.split("--- 消息 ---", 1)[-1]
    if "[当前时间" in body:
        body = body.split("[当前时间", 1)[0]
    body = body.strip()
    i = 0
    while i < len(body) and body[i] in "@＠":
        i += 1
    j = i
    while j < len(body) and body[j].isascii() and (body[j].isalnum() or body[j] in "_-"):
        j += 1
    # 仅「@/装配壳 + ASCII 直接贴非 ASCII」才剥前缀，避免误切英文句首词。
    glued = j > i and j < len(body) and not body[j].isascii() and not body[j].isspace()
    if glued and (had_envelope or i > 0):
        body = body[j:].lstrip()
    return body.strip()


def spoken_user_body_len(text: str) -> int:
    """真人句长度（用于在途短轮瘦工具池，不靠话题词）。"""
    return len(spoken_user_body(text))


def looks_like_status_inquiry(text: str, *, has_active_task: bool) -> bool:
    """用户是否在追问进行中任务进度（结构/催促闭类）。"""
    body = spoken_user_body(text)
    if not body:
        return False
    if _STATUS_INQUIRY_RE.search(body):
        return True
    if has_active_task and len(body) <= 12 and _STATUS_SHORT_NUDGE_RE.match(body):
        return True
    return False


def looks_like_numeric_recitation(text: str) -> bool:
    """台词是否在念多点读数（应上图，不是群聊气泡）。

    形态：多行含数字的对照表，或高密度小数/百分号。不按业务域词分支。
    """
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    n_digits = len(re.findall(r"\d+", body))
    if _fact_lines_multi_point(body) and n_digits >= 5 and len(body) >= 24:
        return True
    if len(body) < 80:
        return False
    nums = re.findall(r"(?<![\d.])(?:\d+\.\d+%?|\d{1,3}%|\d{4,})(?![\d.])", body)
    if len(nums) >= 6:
        return True
    return len(nums) >= 4 and len(body) >= 140


def claims_premature_delivery(text: str) -> bool:
    """是否在宣称交付物已就绪（完成态口吻）。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    return bool(_PREMATURE_DELIVERY_RE.search(body))


def looks_like_empty_handoff(text: str) -> bool:
    """是否空交付/摆烂：声称有料却推诿、不出图也不给要点。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if looks_like_wait_comfort(body):
        return False
    # 长结构正文（多段标题/表）走 report_speech，勿因句尾「先睡了」误判摆烂
    if looks_like_report_speech(body):
        return False
    if len(body) >= 200 and body.count("\n") >= 3:
        return False
    return bool(_EMPTY_HANDOFF_RE.search(body))


def looks_like_process_meta(text: str) -> bool:
    """是否框架/过程元话语（对用户即 OOC）。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    return bool(_PROCESS_META_RE.search(body))


def looks_like_wait_template(text: str) -> bool:
    return (text or "").strip() in IN_FLIGHT_WAIT_TEMPLATES


def looks_like_wait_comfort(text: str) -> bool:
    """是否短等待安慰或「会比较久」委派声明（步骤 3，可发一次）。"""
    body = (text or "").strip()
    if looks_like_wait_template(body):
        return True
    if not body or len(body) > IN_FLIGHT_SPEECH_MAX:
        return False
    if _INFLIGHT_PROCESS_VERB_RE.search(body):
        return False
    # 不得同时是空交付摆烂
    if _EMPTY_HANDOFF_RE.search(body) and not _WAIT_COMFORT_RE.search(body):
        return False
    if claims_premature_delivery(body):
        return False
    return bool(_WAIT_COMFORT_RE.search(body))


def looks_like_inflight_quota_speech(text: str) -> bool:
    """在途台词额度：角色短句，不含过程动词/清单。不定死固定句。"""
    body = (text or "").strip()
    if looks_like_wait_template(body):
        return True
    if not body or len(body) > IN_FLIGHT_SPEECH_MAX:
        return False
    if _INFLIGHT_PROCESS_VERB_RE.search(body):
        return False
    if looks_like_report_speech(body) or looks_like_numeric_recitation(body):
        return False
    if looks_like_empty_handoff(body):
        return False
    if body.count("\n") >= 3:
        return False
    if claims_premature_delivery(body) and not looks_like_wait_comfort(body):
        return False
    if looks_like_wait_comfort(body):
        return True
    if len(_MD_HEADING_RE.findall(body)) >= 1:
        return False
    return True


def has_orchestration_narration(text: str) -> bool:
    """是否在向用户叙述内部编排（worker/节点名）。"""
    body = (text or "").strip()
    if not body:
        return False
    return bool(_ORCHESTRATION_NARRATION_RE.search(body))


def looks_like_report_speech(text: str) -> bool:
    """主人格台词是否呈长结构（应出图而非刷屏）。"""
    body = (text or "").strip()
    if len(body) < 80:
        return False
    if is_silence_marker(body):
        return False
    heads = len(_MD_HEADING_RE.findall(body))
    # 行首加粗小标题（**命名规则** 单独成行）
    bold_lines = len(re.findall(r"(?m)^\*{0,2}\*\*[^*\n]{2,40}\*\*\s*$", body))
    heads = max(heads, bold_lines)
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    pipe_dense = body.count("|") >= 6 and "\n" in body
    if pipe_dense and len(body) >= 120:
        return True
    # 两个及以上小标题 + 一定长度 → 长结构体
    if heads >= 2 and len(body) >= 100:
        return True
    if len(paras) >= 4 and len(body) >= 140:
        return True
    if len(paras) >= 3 and len(body) >= 220:
        return True
    # 单段超长「小作文」
    if len(paras) <= 2 and len(body) >= 500 and (heads >= 1 or body.count("…") + body.count("...") >= 4):
        return True
    if looks_like_numeric_recitation(body):
        return True
    return False


def has_open_solicitation(text: str) -> bool:
    """是否含引导用户继续的收尾邀约（助理腔）。"""
    body = (text or "").strip()
    if not body:
        return False
    # 全文扫描：邀约常夹在中后段，不只在最后一句
    return bool(_OPEN_SOLICIT_RE.search(body))


def strip_open_solicitations(text: str) -> str:
    """去掉引导性追问段，保留事实句。"""
    body = (text or "").strip()
    if not body:
        return body
    parts = re.split(r"(\n\s*\n)", body)
    chunks = [parts[i] for i in range(0, len(parts), 2)]
    out_chunks: list[str] = []
    for ch in chunks:
        c = ch.strip()
        if not c:
            continue
        # 整段是邀约，或段尾以邀约收束 → 丢掉邀约句
        if _OPEN_SOLICIT_RE.search(c) and (
            len(c) < 80 or c.rstrip("…。.！!？?zZ \t").endswith(("吗", "呢", "？", "?")) or "要不要" in c
        ):
            # 若段内前半仍有事实，只砍含邀约的句子
            sents = re.split(r"(?<=[。！？!?\n])", c)
            kept_s = [s for s in sents if s.strip() and not _OPEN_SOLICIT_RE.search(s)]
            c2 = "".join(kept_s).strip()
            if c2:
                out_chunks.append(c2)
            continue
        out_chunks.append(c)
    if not out_chunks:
        return ""
    return "\n\n".join(out_chunks)


def resolve_speech_policy(
    *,
    is_framework: bool,
    fake_done_retry: bool,
    is_status_inquiry: bool,
    has_active_task: bool,
    user_text: str,
) -> SpeechPolicy:
    """根据本轮入口解析话术策略。"""
    if is_framework:
        # 任务完成回灌 vs 纠正 nudge：完成回灌含「任务完成」/产物句柄卡
        low = (user_text or "").lstrip()
        if low.startswith("（系统校验"):
            return "framework_nudge"
        if "任务完成" in low or "子任务交付" in low or "产物句柄" in low:
            return "framework_deliver"
        if low.startswith("[框架·"):
            return "framework_deliver"
        return "framework_nudge"
    if fake_done_retry:
        return "framework_nudge"
    if is_status_inquiry and has_active_task:
        return "status_ok"
    return "free"


def wall_clock_nudge_for(*, need_render_pipeline: bool) -> str:
    return _WALL_CLOCK_PIPELINE if need_render_pipeline else _WALL_CLOCK_CLOSE


def should_block_user_visible_text(
    policy: str,
    text: str,
    *,
    pending_async: bool,
    image_sent: bool,
    has_status_tool: bool,
    tool_calls_so_far: Sequence[str],
    wait_comfort_sent: bool = False,
    fact_pack_pending: bool = False,
    has_active_task: bool = False,
    render_inflight: bool = False,
    speech_len_hard: int = 0,
    user_asked_detail: bool = False,
) -> tuple[bool, str]:
    """是否拦截本段对用户可见文本。返回 (block, reason)。"""
    body = (text or "").strip()
    if not body:
        return True, "empty"
    if is_silence_marker(body):
        return False, "silence"

    pol: SpeechPolicy = (
        policy
        if policy
        in (
            "free",
            "silence_only",
            "status_ok",
            "framework_nudge",
            "framework_deliver",
            "delivered",
        )
        else "free"
    )
    inflight = bool(pending_async or render_inflight)

    # 交付终局：本 run 已经由发送工具交付完毕，对用户只许 <SILENCE>。
    if pol == "delivered":
        return True, "delivered_terminal"

    # 图已发出：放行极短角色收尾；仍拦长结构 / 编排词 / 引导追问
    if image_sent:
        if has_orchestration_narration(body):
            return True, "orchestration_leak"
        if looks_like_delivery_status_narration(body):
            return True, "delivery_narration"
        if looks_like_report_speech(body):
            return True, "report_speech"
        if has_open_solicitation(body) and len(body) > 40:
            return True, "open_solicit"
        if len(body) > 120 and not looks_like_wait_comfort(body):
            return True, "post_image_too_long"
        return False, "post_image_ok"

    # 异步在途：一句短额度（等待或短应）；清单/念白/完成腔不占额度、直接静默
    if inflight or pol == "silence_only":
        if looks_like_inflight_quota_speech(body) and not wait_comfort_sent:
            return False, "wait_comfort"
        return True, "silence_only_or_async"

    if pol == "framework_nudge":
        # 纠正轮：允许「等一下」+ 工具；禁止闲聊抱怨
        if looks_like_wait_comfort(body) and not wait_comfort_sent:
            return False, "wait_comfort"
        return True, "framework_nudge_no_speech"

    if has_orchestration_narration(body):
        return True, "orchestration_leak"

    if looks_like_delivery_status_narration(body):
        return True, "delivery_narration"

    if looks_like_process_meta(body):
        return True, "process_meta"

    if claims_premature_delivery(body) and not image_sent:
        return True, "premature_delivery"

    # 多点读数进气泡 = 该走资料图。不要求已有在途任务（首轮对照同样适用）。
    if not image_sent and looks_like_numeric_recitation(body):
        if not (pol == "status_ok" and has_status_tool):
            return True, "numeric_recitation"

    # 仅当真有待出图材料时：空交付/摆烂才拦截并武装纠正
    if fact_pack_pending and looks_like_empty_handoff(body) and not image_sent:
        return True, "empty_handoff"

    # 长结构台词：仅当**真有待出图事实包**时才拦（该走 render）。
    # 无事实包的长文本是用户点名要的正文（作文/代码/翻译），交呈现层，不拦不改。
    if pol in ("free", "status_ok", "framework_deliver") and fact_pack_pending and looks_like_report_speech(body):
        return True, "report_speech"

    if pol == "framework_deliver":
        # 回灌：未发图前禁止完成腔；发图后允许极短角色句（image_sent 已在上面处理）
        if not image_sent and len(body) > 40 and not looks_like_wait_comfort(body):
            return True, "deliver_before_send_long"
        return False, "ok"

    if pol == "status_ok":
        # 追问进度：应先查工具；零工具却报状态 → 拦（由 settle 再 nudge）
        if not has_status_tool and not tool_calls_so_far and len(body) > 8:
            # 极短「嗯」类放行价值低；进度句必须有工具
            if _STATUS_INQUIRY_RE.search(body) or claims_premature_delivery(body) or len(body) > 15:
                return True, "status_without_tool"
        return False, "ok"

    # 长度只写角色卡。硬拦会吞 by_bot 群聊回复（纠正/INV-4 未接此原因）。
    _ = (speech_len_hard, user_asked_detail)
    return False, "ok"


def content_is_render_candidate(
    *,
    tool_name: str,
    content: str,
    fileos_folded: bool,
) -> bool:
    """是否可作为「待出图事实包」候选（体积折叠 ≠ 可出图）。"""
    body = (content or "").strip()
    if not body:
        return False
    if is_observation_untrusted(body):
        return False
    # 失败/空/加载清单
    if body.startswith("⚠️") or body.startswith("❌"):
        return False
    if "没有找到与" in body[:80] and "工具" in body[:80]:
        return False
    if "抓取失败" in body[:40] or "执行失败" in body[:40]:
        return False
    # 异步 ack 不是终态材料
    if "后台执行" in body or "自动回灌" in body or "仍在执行" in body:
        return False

    tn = (tool_name or "").lower()
    if tn == "create_subagent":
        # 完成交付：足够长且非过程口癖
        if "事实包" in body or "artifact" in body.lower() or re.search(r"\bres_[0-9a-fA-F]{6,}\b", body):
            return len(body) >= 80
        return len(body) >= 200 and ("|" in body or body.count("\n") >= 6)

    # 有 res_ 句柄的长文
    if re.search(r"\bres_[0-9a-fA-F]{6,}\b", body) and len(body) >= 40:
        return True

    # 无时点聚合（气候/月均/历史均值）：不武装出图——把「常年均值」渲成图当答案
    # 正是 P2 编造陷阱，应口头带不确定口径，而非出图坐实（create_subagent 事实包除外）。
    if _is_timeless_aggregate(body):
        return False

    # 检索/抓取类：单点问答可短回，只有**多点**结构才值得出图（4.2）
    _searchish = any(h in tn for h in ("search", "web_", "fetch", "knowledge"))
    if _searchish and not _fact_lines_multi_point(body):
        return False

    # 真表。长散文不得单独武装（观测说明文误报）。
    if "|" in body and body.count("\n") >= 3:
        return True

    # FileOS 折叠：用原文形态判定。多点事实行已在上面过门，折叠后仍要武装出图。
    if fileos_folded:
        if _fact_lines_multi_point(body) and len(body) >= 80:
            return True
        if "事实包" in body and len(body) >= 80:
            return True
        if "|" in body and body.count("\n") >= 3 and len(body) >= 200:
            return True
        if body.count("\n\n") >= 3 and len(body) >= 600:
            return True
        return False

    # 搜索类：与旧口径类似但更严；单点检索默认不武装出图
    if _searchish:
        if "|" in body and body.count("\n") >= 4 and len(body) >= 600:
            return True
        if body.count("\n\n") >= 3 and len(body) >= 1200:
            return True
        # 多点逐行观测：形态达标即武装，不卡绝对长度
        if len(body) >= 80:
            return True
        return False
    return False


def is_status_tool_name(name: str) -> bool:
    n = (name or "").lower()
    return (
        n
        in {
            "list_my_kanban_tasks",
            "artifact_get_recent",
            "artifact_list",
            "artifact_get",
            "list_persisted_outputs",
            "grep_persisted_outputs",
            "search_cognition",
            "read_handle",
            "list_my_tasks",
        }
        or "kanban" in n
    )
