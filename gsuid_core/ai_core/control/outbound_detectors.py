"""无状态出站话术与 OOC 检测器（控制面叶子检测层）。

集中存放 ``speech_policy``、``output_gate``、``control.policy_ring`` 与 ``gs_agent``
共用的纯文本判别正则、结构谓词及 ``FirewallHit`` 类型，消除控制层反向依赖
``agent_run`` 的层级倒置。状态包、重说调度与闸门策略表见 ``policy_ring`` 与
``output_gate``。
"""

from __future__ import annotations

import re
from typing import List, Tuple, Literal, Optional, Sequence
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Callable

from gsuid_core.config import core_config
from gsuid_core.ai_core.content_guard import normalize_for_match
from gsuid_core.ai_core.control.lines import (
    CODE_FENCE_RE,
    MD_LIST_ITEM_RE,
    INTERNAL_LEAK_RE,
    MSG_SECTION_MARK,
    OOC_JUDGE_MARKER,
    FUND_CLAIM_PREFIX,
    INTERNAL_HANDLE_RE,
    TOOL_NAME_LEAK_NUDGE,
    INTERNAL_HANDLE_HINTS,
    HANDLE_IMAGE_LEAK_NUDGE,
    CAPABILITY_OR_DEV_LEAK_RE,
)
from gsuid_core.ai_core.protocol_text import is_silence_marker
from gsuid_core.ai_core.control.angle_guard import (
    MEME_TAG_PATTERN,
    mask_code_spans,
    strip_protocol_tags,
)
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    fact_pack_is_multi_point as _fact_lines_multi_point,
)


@dataclass(slots=True)
class OutboundTextView:
    """单次出站文本预处理视图：惰性缓存剥壳、代码掩码与规范化文本，避免跨闸门重复扫描。"""

    raw: str
    _stripped: Optional[str] = None
    _protocol_stripped: Optional[str] = None
    _code_masked: Optional[str] = None
    _fence_stripped: Optional[str] = None
    _norm: Optional[str] = None

    @property
    def stripped(self) -> str:
        if self._stripped is None:
            self._stripped = (self.raw or "").strip()
        return self._stripped

    @property
    def protocol_stripped(self) -> str:
        if self._protocol_stripped is None:
            self._protocol_stripped = strip_protocol_tags(self.raw)
        return self._protocol_stripped

    @property
    def code_masked(self) -> str:
        if self._code_masked is None:
            self._code_masked = mask_code_spans(self.protocol_stripped) if "<" in self.raw else self.protocol_stripped
        return self._code_masked

    @property
    def fence_stripped(self) -> str:
        if self._fence_stripped is None:
            self._fence_stripped = CODE_FENCE_RE.sub(" ", self.raw) if "```" in self.raw else self.raw
        return self._fence_stripped

    @property
    def norm(self) -> str:
        if self._norm is None:
            self._norm = normalize_for_match(self.raw)
        return self._norm


# ── A. 出站话术无状态检测器 ────────────────────────────────────────────────


# 在途极短安慰额度（委派后的第二句起）。不定死话术；过程动词/第二执行者/清单仍拦。
IN_FLIGHT_SPEECH_MAX = 12
# 开场接任务应：不按 12 字卡。长度用档位上限；结构垃圾才拦。
FIRST_ACK_SPEECH_MAX = 150
IN_FLIGHT_WAIT_TEMPLATES: tuple[str, ...] = ("马上好。", "嗯，在弄了。")

_STATUS_INQUIRY_RE = re.compile(
    r"(好了吗|好了没|弄好了吗|弄好没|完成了吗|画好了吗|出好了吗|"
    r"还好了没|怎么样了|咋样了|如何了|进度|还要多久|要多久|"
    r"好了\s*[？?]?$|弄好了\s*[？?]?$|图呢|结果呢|好了没啊|人呢[？?]?|人呐[？?]?)",
    re.IGNORECASE,
)
_STATUS_SHORT_NUDGE_RE = re.compile(r"^[\s…·.。!！?？嗯啊呢哈]+$|^(呢|啊|？|\?|嗯\??|然后呢|后来呢)$")

_PREMATURE_DELIVERY_RE = re.compile(
    r"(出了个?图|(?:画|弄|做|生成|渲染|图)好了|"
    r"已经(?:画|弄好|出图)|详情让.{0,12}出了|"
    r"整理好了|(?:翻|弄|查)完了)",
    re.IGNORECASE,
)

_EMPTY_HANDOFF_RE = re.compile(
    r"(念不(动|完|下)|懒得念|太长了.{0,10}(不|懒|念)|"
    r"要哪段|再喊我|"
    r"都在里面了(?!…?图))",
    re.IGNORECASE,
)

_REDIRECT_EXECUTOR_RE = re.compile(
    r"(?:别的家伙|另一个机器人|那个机器人).{0,16}(?:管|干|指令|命令)"
    r"|(?:那种指令|那种命令).{0,16}(?:别的家伙|另一个机器人|那个机器人)"
    r"|(?:你直接发|先发)\s*(?:[`]|[「『\"]\s*/)"
)
_TODAY_NOW_RE = re.compile(r"今天|今日|现在|此刻")
_YMD_RE = re.compile(r"(20\d{2})[-年./](\d{1,2})[-月./](\d{1,2})")
_REMINISCE_RE = re.compile(r"想起|记得|回忆|想想|那年|那天|那次|那会儿|那时候")
_HISTORICAL_LOOKUP_RE = re.compile(r"看了|查了|翻了|回看|历史|记录|存档|当时|那会儿")
_METRIC_VALUE_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:[%‰℃°]|度|元|块|分|点|倍|个|条|次|人|ms|KB|MB|GB))|(?:[¥$€]\s*\d)"
)

_DELIVERY_REPORT_RE = re.compile(
    r"(任务|图(片)?|消息|产物|结果|数据)[^。！？\n]{0,8}(已完成|完成|已发送|已发给|已交付|已发出|发送完毕|已经发送|已经完成)"
    r"|(已|已经)?(发送给|发给|发到)\s*[^\s，。！？]{1,12}",
    re.IGNORECASE,
)
_DELIVERY_META_CLOSE_RE = re.compile(
    r"(无需|不必|不用|无须)[^。！？\n]{0,6}(追加|补充|再?发言|再?回复|多说|多言|赘述)",
    re.IGNORECASE,
)
_INFLIGHT_PROCESS_VERB_RE = re.compile(r"(还在(渲|画|跑|查)|在渲|查中|还没(画|渲|写)好)")

_WAIT_COMFORT_RE = re.compile(
    r"(等(一?下|会|会儿)|稍等|先等|等我|慢点|"
    r"(画|翻|弄|查|整).{0,6}(一下|会儿)|"
    r"先(翻|画|弄)|马上|很快|"
    r"这就(去|来)?(翻|弄|查|办|看|整)|"
    r"(比较|有点|会)?(久|慢|费时|花(点|些)?时间)|"
    r"耐心|等着|先等着|得翻|得查|得弄|翻会儿|查会儿|"
    r"别急|慢慢|稍后|等等我|"
    r"先让我|还缺|再查|接着查|继续查|先查)",
    re.IGNORECASE,
)

_ORCHESTRATION_NARRATION_RE = re.compile(
    r"(让|叫|派)\s*[A-Za-z][A-Za-z0-9_]{2,}\s*(去|来|出|画|渲|跑|执行|处理)|"
    r"(让|叫|派)\s*(?!我|自己|你)(?:[^\s，。！？、]{0,4}(?:agent|worker|帮手|助手|后台|代理|子任务|节点|工具|系统)|[A-Za-z][A-Za-z0-9_]{2,})\s*(去|来|出|画|渲|跑|执行|处理|查)|"
    r"\brender\b.{0,6}(出|画|渲)|"
    r"(出|画|渲).{0,6}\brender\b",
    re.IGNORECASE,
)

_MD_HEADING_RE = re.compile(r"(?m)^#{1,3}\s+\S|^\*{0,2}\*\*[^*\n]{2,48}\*\*")

_DELIVER_PROGRESS_RE = re.compile(
    r"别催了?|还在(?:跑|查|弄|画|等结果)|还没(?:跑完|查完|出结果|弄好)|再等.{0,4}(?:就好|马上)"
)


def looks_like_delivery_status_narration(text: str) -> bool:
    """是否交付状态汇报（交付播报 × 行政化自我静默同段共现）。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if _DELIVERY_META_CLOSE_RE.search(body) is None:
        return False
    return _DELIVERY_REPORT_RE.search(body) is not None


def spoken_user_body(text: str) -> str:
    """剥装配外壳后的真人句。通道粘连的 ASCII 唤醒词不是话题。"""
    body = (text or "").strip()
    if not body:
        return ""
    had_envelope = MSG_SECTION_MARK in body
    if had_envelope:
        body = body.split(MSG_SECTION_MARK, 1)[-1]
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


_CALENDAR_YEAR_RE = re.compile(r"(?<![\d.])20\d{2}(?=\s*年|[-/.]\d{1,2})")


def looks_like_numeric_recitation(text: str) -> bool:
    """台词是否在念多点读数（多行数字对照表或高密度小数/百分号）。"""
    body = strip_protocol_tags(text or "").strip()
    if not body or is_silence_marker(body):
        return False
    scrubbed = _CALENDAR_YEAR_RE.sub("", body)
    n_digits = len(re.findall(r"\d+", scrubbed))
    if _fact_lines_multi_point(body) and n_digits >= 5 and len(body) >= 24:
        return True
    if len(body) < 80:
        return False
    nums = re.findall(r"(?<![\d.])(?:\d+\.\d+%?|\d{1,3}%|\d{4,})(?![\d.])", scrubbed)
    if len(nums) >= 6:
        return True
    return len(nums) >= 4 and len(body) >= 140


def claims_premature_delivery(
    text: str,
    *,
    has_fact_pack: bool = False,
    tool_called_this_run: bool = False,
    is_delivery_wake: bool = False,
) -> bool:
    """是否在无事实载体时宣称交付物已就绪。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if has_fact_pack or tool_called_this_run or is_delivery_wake:
        return False
    return bool(_PREMATURE_DELIVERY_RE.search(body))


def looks_like_empty_handoff(text: str) -> bool:
    """是否空交付/摆烂：声称有料却推诿、不出图也不给要点。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if looks_like_wait_comfort(body):
        return False
    if looks_like_report_speech(body):
        return False
    if len(body) >= 200 and body.count("\n") >= 3:
        return False
    return bool(_EMPTY_HANDOFF_RE.search(body))


def user_speaks_latin(user_text: str) -> bool:
    """用户当轮提问本身为英文/拉丁语系时，英文答复不判机器独白。"""
    from gsuid_core.ai_core.interaction_scaffold import extract_message_body

    body = extract_message_body(user_text or "").strip()
    if not body:
        return False
    latin = 0
    for ch in body:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            return False
        if 65 <= o <= 90 or 97 <= o <= 122:
            latin += 1
    return latin >= 6


def looks_like_machine_latin(text: str, *, user_text: str = "") -> bool:
    """长句拉丁字母占比过高：机器独白，不是 IM 角色句。纯链接或英文对话放过。"""
    if user_text and user_speaks_latin(user_text):
        return False
    body = (text or "").strip()
    if len(body) < 40:
        return False
    low = body.lower()
    if body.count(" ") < 4 and ("http://" in low or "https://" in low or "/" in body):
        return False
    n = 0
    for ch in body:
        o = ord(ch)
        if 65 <= o <= 90 or 97 <= o <= 122:
            n += 1
    return n * 5 >= len(body) * 3


def looks_like_process_meta(text: str, *, user_text: str = "") -> bool:
    """是否框架/过程元话语（对用户即 OOC）。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if looks_like_machine_latin(body, user_text=user_text):
        return True
    return bool(INTERNAL_LEAK_RE.search(body))


def _configured_command_starts() -> list[str]:
    raw = core_config.get_config("command_start")
    if not isinstance(raw, list):
        return []
    return [p for p in raw if isinstance(p, str) and p.strip()]


def looks_like_capability_absence(text: str) -> bool:
    """是否在对用户讲自身能力集合缺失，或把办事推给另一套指令/机器人。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    if CAPABILITY_OR_DEV_LEAK_RE.search(body) is not None:
        return True
    if _REDIRECT_EXECUTOR_RE.search(body) is not None:
        return True
    starts = _configured_command_starts()
    if starts:
        escaped = "|".join(re.escape(p) for p in starts)
        if re.search(rf"(?:你直接发|先发)\s*[「『\"]\s*(?:{escaped})", body) is not None:
            return True
    return False


def _clause_around(text: str, idx: int) -> str:
    start = 0
    end = len(text)
    for i in range(idx - 1, -1, -1):
        if text[i] in "。！？\n；":
            start = i + 1
            break
    for i in range(idx, len(text)):
        if text[i] in "。！？\n；":
            end = i
            break
    return text[start:end]


def looks_like_stale_present_tense(text: str, *, now: datetime | None = None) -> bool:
    """是否把过期年月日数值读数说成今天/现在的实况。回忆/回看历史、纯日期不判。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    clock = now if now is not None else datetime.now()
    today = clock.date()
    for m in _YMD_RE.finditer(body):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dated = datetime(year, month, day).date()
        except ValueError:
            continue
        if dated >= today:
            continue
        clause = _clause_around(body, m.start())
        if _TODAY_NOW_RE.search(clause) is None:
            continue
        if _REMINISCE_RE.search(clause) is not None:
            continue
        win_lo = max(0, m.start() - 12)
        win_hi = min(len(body), m.end() + 16)
        window = body[win_lo:win_hi]
        if _HISTORICAL_LOOKUP_RE.search(window) is not None:
            continue
        if _METRIC_VALUE_RE.search(window) is not None:
            return True
    return False


def looks_like_wait_template(text: str) -> bool:
    return (text or "").strip() in IN_FLIGHT_WAIT_TEMPLATES


def looks_like_wait_comfort(text: str) -> bool:
    """是否极短等待安慰或第一人称「这就去办」（可发一次）。"""
    body = (text or "").strip()
    if looks_like_wait_template(body):
        return True
    if not body or len(body) > IN_FLIGHT_SPEECH_MAX:
        return False
    if has_orchestration_narration(body):
        return False
    if _INFLIGHT_PROCESS_VERB_RE.search(body):
        return False
    if claims_premature_delivery(body):
        return False
    return bool(_WAIT_COMFORT_RE.search(body))


def keeps_working(body: str) -> bool:
    """一句里还在查，不算已经交付。空交付摆烂不算。"""
    if looks_like_empty_handoff(body):
        return False
    if looks_like_wait_comfort(body):
        return True
    return _WAIT_COMFORT_RE.search(body) is not None


def _accept_lines(body: str) -> list[str]:
    """接话行数。单独一行的表情标记不是第二段正文。"""
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not MEME_TAG_PATTERN.sub("", line).strip():
            continue
        lines.append(line)
    return lines


def _line_is_action_beat(line: str) -> bool:
    return (line.startswith("（") and line.endswith("）")) or (line.startswith("(") and line.endswith(")"))


def looks_like_task_accept_speech(text: str, *, max_len: int = 0, user_text: str = "") -> bool:
    """开场接任务应：一句角色发言，表示接下来去做；``max_len≤0`` 用默认上限。"""
    body = (text or "").strip()
    if looks_like_wait_template(body):
        return True
    if not body or is_silence_marker(body):
        return False
    cap = max_len if max_len > 0 else FIRST_ACK_SPEECH_MAX
    if len(body) > cap:
        return False
    if has_orchestration_narration(body):
        return False
    if _INFLIGHT_PROCESS_VERB_RE.search(body):
        return False
    if looks_like_report_speech(body) or looks_like_numeric_recitation(body):
        return False
    if looks_like_empty_handoff(body):
        return False
    if looks_like_process_meta(body, user_text=user_text):
        return False
    if looks_like_capability_absence(body):
        return False
    if looks_like_stale_present_tense(body):
        return False
    if looks_like_machine_latin(body, user_text=user_text):
        return False
    content_lines = _accept_lines(body)
    if len(content_lines) > 2:
        return False
    # 两行只放行「括号动作 + 台词」。两段正文仍是结构垃圾。
    if len(content_lines) == 2 and not any(_line_is_action_beat(ln) for ln in content_lines):
        return False
    if claims_premature_delivery(body) and not keeps_working(body):
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
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    pipe_dense = body.count("|") >= 6 and "\n" in body
    if pipe_dense and len(body) >= 120:
        return True
    if heads >= 2 and len(body) >= 100:
        return True
    if len(MD_LIST_ITEM_RE.findall(body)) >= 4 and len(body) >= 180:
        return True
    if len(paras) <= 2 and len(body) >= 500 and (heads >= 1 or body.count("…") + body.count("...") >= 4):
        return True
    if looks_like_numeric_recitation(body):
        return True
    return False


def title_mentioned(title: str, body: str) -> bool:
    """称呼是否出现在台词里。短于两字或夹在更长英文词中的不算。"""
    token = (title or "").strip()
    if len(token) < 2:
        return False
    text = body or ""
    if token.isascii():
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", text, re.IGNORECASE) is not None
    return token in text


def looks_like_deliver_progress(text: str) -> bool:
    """交付回灌里的进度/催促句。短收尾不走这里。"""
    body = (text or "").strip()
    if not body:
        return False
    if looks_like_wait_comfort(body) or looks_like_process_meta(body):
        return True
    if looks_like_delivery_status_narration(body):
        return True
    return _DELIVER_PROGRESS_RE.search(body) is not None


# ── B. 出戏（OOC）无状态检测器与类型 ──────────────────────────────────────


@dataclass
class FirewallHit:
    """出戏命中：类别 + 命中片段（供警告文案与日志）。"""

    category: str
    matched: List[str]


# ToolContext.extra 键：本轮已暴露的工具名集合（装配池 ∪ find_tools）。
EXPOSED_TOOLS_EXTRA_KEY = "exposed_tool_names"


def is_ooc_firewall_enabled() -> bool:
    """出戏防火墙配置总开关。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("output_firewall_enable").data)


# 屏蔽后交给模型的中性说明（非用户可见台词）
TECH_DUMP_TOOL_SHIELD = (
    "（工具返回了技术错误/堆栈，已屏蔽。禁止复述 JSON/Traceback；请用角色短句表示稍后再试，或换路重试工具。）"
)

_MODEL_TERMS: Tuple[str, ...] = (
    "mimo",
    "minimax",
    "gpt",
    "claude",
    "gemini",
    "通义",
    "千问",
    "qwen",
    "文心",
    "豆包",
    "星火",
    "kimi",
    "deepseek",
    "深度求索",
    "小爱",
    "siri",
    "openai",
    "anthropic",
    "小米大模型",
    "chatgpt",
    "llama",
    "grok",
    "moonshot",
    "月之暗面",
    "copilot",
    "mistral",
    "mixtral",
    "deepmind",
    "perplexity",
    "chatglm",
    "internlm",
    "hailuo",
    "stepfun",
    "baichuan",
    "零一万物",
    "智谱",
    "hunyuan",
    "讯飞",
    "kling",
    "midjourney",
    "stablediffusion",
    "火山方舟",
    "华为盘古",
    "openrouter",
    "characterai",
)

_SYSTEM_TERMS: Tuple[str, ...] = (
    "systemprompt",
    "系统提示词",
    "max_tokens",
    "maxtokens",
    "主人格",
    "能力代理",
    "子代理",
    "转译",
)

_TECH_DUMP_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|File \"[^\"]+\", line \d+"
    r"|\bstatus_code\s*[:=]\s*[45]\d\d\b"
    r"|[\"']status[\"']\s*:\s*[45]\d\d"
    r"|\bat 0x[0-9a-fA-F]+\b"
    r"|pydantic_core|pydantic_ai\.",
    re.IGNORECASE,
)

_CTX_TECH_SELF_RE = re.compile(
    r"(我|人家|咱们?|本喵|本人)(的|这边的?)?\s*(训练数据|训练语料|参数量|知识截止|上下文窗口|采样参数"
    r"|api\s*密钥|api\s*-?key)",
    re.IGNORECASE,
)

_MODEL_ATTRIB_RE = re.compile(r"由.{0,10}(开发|训练|研发|提供|打造)")
_AI_COMPOUND = r"(?![绘画艺工插音视翻领行圈技产应从研专课竞赛员师圈])"
_AI_ENTITY_CORE = rf"ai{_AI_COMPOUND}|人工智能{_AI_COMPOUND}|语言模型|大模型"
_AI_ENTITY_BOT = rf"{_AI_ENTITY_CORE}|聊天机器人|机器人"
_AI_SELFREF_RE = re.compile(
    r"我(是|叫|本质上是|其实是|就是|确实是|确实叫|不过是|只是|是一个|是个|作为)(一个|一名|个)?[^，。！？帮给替]{0,8}"
    rf"({_AI_ENTITY_BOT}|程序(?!员)|算法模型|模型(?![玩爱收手师]))",
    re.IGNORECASE,
)
_AI_ASA_RE = re.compile(rf"作为(一个|一名)?.{{0,4}}({_AI_ENTITY_CORE})", re.IGNORECASE)
_AI_ADMIT_RE = re.compile(
    rf"(?:^|[\n。！？!?；;]\s*)(?:唔+[….,，]*\s*|好吧[，,]?\s*|确实[，,]?\s*|其实[，,]?\s*)*"
    rf"就?是\s*(?:一?个)?({_AI_ENTITY_BOT})",
    re.IGNORECASE,
)
_AI_PEER_RE = re.compile(
    r"(大模型|语言模型|人工智能|\bai\b|聊天机器人|机器人)[^。，,！!？?]{0,8}(包括|含|例如|像|比如)[^。，,]{0,4}我"
    r"|我们(这些|这类|这种)[^。，,]{0,4}(大模型|语言模型|人工智能|\bai\b)",
    re.IGNORECASE,
)
_SK_KEY_RE = re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9]{8,}")
_ERR_CODE_RE = re.compile(r"(错误码|报错码|error\s*code)[\s:：]*\d+", re.IGNORECASE)
_SAMPLING_PARAM_RE = re.compile(r"temperature.{0,6}[0-2]\.\d", re.IGNORECASE)
_SELF_BIND_RE = re.compile(
    r"(我|人家|咱|本(喵|人|机|体))[^。！？\n吃喝买点说聊讲玩家]{0,6}(是|用|叫|基于|背后|底层|内核|驱动|跑在|搭载)"
    r"|(?:^|[，。！？!?,\s：:、~～…—])(用的|基于|搭载|采用)的?是"
    r"|(?:^|[，。！？!?,\s：:、~～…—])(模型|底层|内核|后台|本体)[^。！？\n]{0,4}(是|叫|用)",
    re.IGNORECASE,
)
_IDENTITY_PROBE_RE = re.compile(
    r"(什么|哪个|哪家|谁家|啥)[^。！？\n]{0,4}(模型|大模型|llm)"
    r"|谁(开发|研发|训练|做|造|写)的"
    r"|你(就?是不是|真的是|到底是|就?是)[^。！？\n]{0,4}(ai|人工智能|机器人|大?模型|真人|gpt|程序|chatgpt|bot)"
    r"|(承认|坦白|老实(说|交代))[^。！？\n]{0,8}(是)?(ai|人工智能|机器人|大?模型|真人|bot)"
    r"|(底层|内核|背后|本体)[^。！？\n]{0,4}(是|用)(什么|啥|哪)"
    r"|what\s+model|which\s+model|are\s+you\s+(an?\s+)?(ai|bot|gpt|llm)",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?\n；;，,]")

_MONEY_TERM_RE = re.compile(
    r"钱|款项|红包|转账|打款|汇款|(?<![a-z0-9])v\d{1,4}(?![\d.a-z])|\d+\s*[块元]|微信支付|支付宝",
    re.IGNORECASE,
)
_TRANSFER_DONE_RE = re.compile(
    r"(?:已经?|刚刚?|明明|都)?(?:(?:转|汇|付)(?:过去|给你|完|出去|款)?|(?:打|发)(?:过去|给你|完|出去|款))了"
    r"|红包[^\n，。！？!?；;]{0,4}发了|发了[^\n，。！？!?；;]{0,2}红包"
)
_AT_FUND_REQUEST_RE = re.compile(
    r"@\d{5,}[^\n]{0,40}?(?:能不能|求|给|发|来个|支援)[^\n]{0,10}?(?:v\d{1,4}(?![\d.a-z])|红包|\d+\s*(?:块钱|元钱)|点?钱)",
    re.IGNORECASE,
)
_FUND_DEMAND_STRONG_RE = re.compile(
    r"钱呢|红包呢|没收到|收到了?吗|转了吗|付了吗|打钱|(?<![a-z0-9])v\d{1,4}(?![\d.a-z])|打过?来|转过?来",
    re.IGNORECASE,
)
_FUND_DEMAND_FORM_RE = re.compile(r"呢|了吗|过?来|快点|还不")

_META_SELF_MECH_RE = re.compile(
    r"(内部|后台|服务器|数据库|引擎|系统)"
    r"|(我|咱|俺|人家|本人|本喵|本机)[^。！？\n，,；;]{0,6}(这边|这头|底下|手里|手头|手上|这儿)"
)
_MECH_NOUN = r"(?:内部|后台|资料|知识|记忆|数据|私有|本地|检索|素材|云端)库|数据表|检索层|检索器|词表|提示词|数据源"
_META_MECH_NOUN = _MECH_NOUN + r"|档案|台账|索引|条目|日志|缓存|上下文"
_MECH_NOUN_RE = re.compile(_MECH_NOUN)
_META_MECH_NOUN_RE = re.compile(_META_MECH_NOUN)
_ABSENT_CORE = r"没(?!有?(?:问题|异常|关系|事|必要|意思|兴趣|毛病))|未(?!来)|尚未|不存在|是空"
_ABSENT_TERM = rf"(?:{_ABSENT_CORE})"
_MECH_ABSENT_RE = re.compile(
    rf"(?:{_MECH_NOUN})[^。！？\n，,；;]{{0,6}}{_ABSENT_TERM}"
    rf"|{_ABSENT_TERM}[^。！？\n，,；;]{{0,6}}(?:{_MECH_NOUN})"
)
_META_ABSENT_TERM = (
    rf"(?:{_ABSENT_CORE}|无(?!所谓|论|声|法)|并非|并不"
    r"|不(?:太|怎么|大)?(?:全(?!是|都|对)|完整|准(?!备)|精确|齐|新鲜|覆盖|留(?!情)|保存|存(?!放)|记得)"
    r"|(?:查|搜|找|提|拿|取)不到|找不到|查不着)"
)
_META_MECH_ABSENT_RE = re.compile(
    rf"(?:{_META_MECH_NOUN})[^。！？\n，,；;]{{0,6}}{_META_ABSENT_TERM}"
    rf"|{_META_ABSENT_TERM}[^。！？\n，,；;]{{0,6}}(?:{_META_MECH_NOUN})"
)
_EXPOSED_TOOL_TOKEN_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_]{2,})\b")
_FRAMEWORK_TECH_RE = re.compile(r"(FastAPI|WebSocket|框架|插件系统|Python|SQLAlchemy)")


def _any_clause(text: str, pred: Callable[[str], bool]) -> bool:
    return any(bool(seg) and pred(seg) for seg in _CLAUSE_SPLIT_RE.split(text))


def _fund_demand_context(user_text: str) -> bool:
    if _FUND_DEMAND_STRONG_RE.search(user_text):
        return True
    return _any_clause(user_text, lambda s: bool(_MONEY_TERM_RE.search(s) and _FUND_DEMAND_FORM_RE.search(s)))


def _fund_claim_hit(text: str, user_text: str = "") -> Optional[str]:
    if _AT_FUND_REQUEST_RE.search(text):
        return "代向第三方索要钱财"
    if _any_clause(text, lambda s: bool(_MONEY_TERM_RE.search(s) and _TRANSFER_DONE_RE.search(s))):
        return "声称已完成转账/付款"
    if user_text and _fund_demand_context(user_text) and _TRANSFER_DONE_RE.search(text):
        return "催款语境下声称已付款"
    return None


def _self_bound_model_leak(text: str, extra_terms: Tuple[str, ...]) -> bool:
    def _seg_binds(seg: str) -> bool:
        if _SELF_BIND_RE.search(seg) is None:
            return False
        norm_seg = normalize_for_match(seg)
        if any(normalize_for_match(w) and normalize_for_match(w) in norm_seg for w in (*_MODEL_TERMS, *extra_terms)):
            return True
        return _MODEL_ATTRIB_RE.search(seg) is not None

    return _any_clause(text, _seg_binds)


def looks_like_meta_narration(text: str) -> bool:
    """台词是否把自身检索机制当成记忆事实讲出去（同小句三信号共现）。"""

    def _meta_clause(seg: str) -> bool:
        if _META_SELF_MECH_RE.search(seg) is None:
            return False
        norm_seg = normalize_for_match(seg)
        return bool(_META_MECH_NOUN_RE.search(norm_seg) and _META_MECH_ABSENT_RE.search(norm_seg))

    return _any_clause(text, _meta_clause)


def _mechanism_absence_clause(text: str) -> bool:
    return _any_clause(text, lambda s: bool(_MECH_NOUN_RE.search(normalize_for_match(s)) and _MECH_ABSENT_RE.search(s)))


def _extra_terms() -> Tuple[str, ...]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    data = ai_config.get_config("output_firewall_extra_terms").data
    if isinstance(data, list):
        return tuple(str(x) for x in data if str(x).strip())
    return ()


def _exposed_tool_name_leak(text: str, names: Sequence[str]) -> str | None:
    if not text or not names:
        return None
    lower_map = {n.lower(): n for n in names if n}
    if not lower_map:
        return None
    for m in _EXPOSED_TOOL_TOKEN_RE.finditer(text):
        key = m.group(1).lower()
        if key in lower_map:
            return lower_map[key]
    return None


def _framework_alias_list() -> list[str]:
    raw = core_config.get_config("framework_aliases")
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw if str(x).strip()]
    return ["GsCore", "gsuid_core"]


def _objective_framework_intro(text: str) -> bool:
    if "是" not in text or not _FRAMEWORK_TECH_RE.search(text):
        return False
    return any(alias in text for alias in _framework_alias_list() if alias)


def is_tech_dump(text: str) -> bool:
    """工具/子代理返回是否为堆栈或状态码技术 dump（供 tool return 入模前屏蔽）。"""
    if not text or not text.strip():
        return False
    # 认知/记忆检索的历史对话引用含用户曾粘贴的报错，不属于当前工具异常堆栈
    if '<untrusted source="memory_recall">' in text or "【认知检索】" in text:
        return False
    if _TECH_DUMP_RE.search(CODE_FENCE_RE.sub(" ", text)):
        return True
    # 大段 JSON 且含 status + error/detail 形态
    s = text.strip()
    if (
        s.startswith("{")
        and ('"status"' in s or "'status'" in s)
        and ("error" in s.lower() or "traceback" in s.lower() or "detail" in s.lower())
    ):
        return True
    return False


@dataclass(slots=True)
class OocScanContext:
    """单次 OOC 检测上下文：复用 ``OutboundTextView`` 并惰性缓存扩展词表与身份追问标志。"""

    text: str
    user_text: str = ""
    exposed_tool_names: Sequence[str] = ()
    view: Optional[OutboundTextView] = None
    _norm: Optional[str] = None
    _extra: Optional[Tuple[str, ...]] = None
    _probing: Optional[bool] = None

    @property
    def norm(self) -> str:
        if self.view is not None:
            return self.view.norm
        if self._norm is None:
            self._norm = normalize_for_match(self.text)
        return self._norm

    @property
    def fence_stripped(self) -> str:
        if self.view is not None:
            return self.view.fence_stripped
        return CODE_FENCE_RE.sub(" ", self.text) if "```" in self.text else self.text

    @property
    def extra_terms(self) -> Tuple[str, ...]:
        if self._extra is None:
            self._extra = _extra_terms()
        return self._extra

    @property
    def probing(self) -> bool:
        if self._probing is None:
            self._probing = bool(self.user_text) and _IDENTITY_PROBE_RE.search(self.user_text) is not None
        return self._probing


def match_ooc_delivery_narration(ctx: OocScanContext) -> list[str] | None:
    return ["交付状态汇报"] if looks_like_delivery_status_narration(ctx.text) else None


def match_ooc_meta_narration(ctx: OocScanContext) -> list[str] | None:
    return ["内部机制自述"] if looks_like_meta_narration(ctx.text) else None


def match_ooc_capability_absence(ctx: OocScanContext) -> list[str] | None:
    if looks_like_capability_absence(ctx.text) or _mechanism_absence_clause(ctx.text):
        return ["能力缺失叙述"]
    return None


def match_ooc_stale_present(ctx: OocScanContext) -> list[str] | None:
    return ["过期时点当现在"] if looks_like_stale_present_tense(ctx.text) else None


def match_ooc_ai_selfref(ctx: OocScanContext) -> list[str] | None:
    if _AI_SELFREF_RE.search(ctx.text) or _AI_ASA_RE.search(ctx.text) or _AI_PEER_RE.search(ctx.text):
        return ["AI自指"]
    if ctx.probing and _AI_ADMIT_RE.search(ctx.text):
        return ["AI自指(认领)"]
    return None


def match_ooc_fund_claim(ctx: OocScanContext) -> list[str] | None:
    hit = _fund_claim_hit(ctx.text, ctx.user_text)
    return [hit] if hit is not None else None


def match_ooc_machine_dump(ctx: OocScanContext) -> list[str] | None:
    if _TECH_DUMP_RE.search(ctx.fence_stripped):
        return ["技术堆栈/状态码"]
    return None


def match_ooc_model_identity(ctx: OocScanContext) -> list[str] | None:
    model_hits = [
        w for w in (*_MODEL_TERMS, *ctx.extra_terms) if normalize_for_match(w) and normalize_for_match(w) in ctx.norm
    ]
    if not (model_hits or _MODEL_ATTRIB_RE.search(ctx.text)):
        return None
    # 精度门：裸词/"由…开发"须与自绑定句式同小句共现，或身份追问下的超短直答才算泄露；
    # 长文本第三方提及（AI 新闻摘要/讨论）放行。
    short_direct = len(ctx.norm) <= 24 and ctx.probing
    if short_direct or _self_bound_model_leak(ctx.text, ctx.extra_terms):
        return model_hits or ["由…开发"]
    return None


def match_ooc_internal_handle_leak(ctx: OocScanContext) -> list[str] | None:
    """内部资源/落盘/笔记句柄永远不属于用户台词，独立于软出戏开关始终拦截。"""
    lower = ctx.text.lower()
    if not any(h in lower for h in INTERNAL_HANDLE_HINTS):
        return None
    found = INTERNAL_HANDLE_RE.findall(ctx.text)
    if not found:
        return None
    return [f"内部句柄:{h[:40]}" for h in found[:4]]


def match_ooc_system_term(ctx: OocScanContext) -> list[str] | None:
    system_hits = [w for w in _SYSTEM_TERMS if normalize_for_match(w) in ctx.norm]
    if _CTX_TECH_SELF_RE.search(ctx.text):
        system_hits.append("第一人称技术自述")
    if _SK_KEY_RE.search(ctx.text):
        system_hits.append("密钥")
    if _ERR_CODE_RE.search(ctx.text):
        system_hits.append("错误码")
    if _SAMPLING_PARAM_RE.search(ctx.text):
        system_hits.append("temperature")
    leak = INTERNAL_LEAK_RE.search(ctx.text)
    if leak is not None:
        system_hits.append(f"框架泄漏:{leak.group(0)[:40]}")
    tool_leak = _exposed_tool_name_leak(ctx.text, ctx.exposed_tool_names)
    if tool_leak is not None:
        system_hits.append(f"框架泄漏:{tool_leak}")
    if system_hits:
        return system_hits
    if _objective_framework_intro(ctx.text):
        return ["客观介绍宿主框架"]
    return None


# ── C. 数据驱动 OOC 规则环（检测优先级 + 拦截强度 + 重说提醒） ─────────────


OocSeverity = Literal["fuse", "never_release_defer", "remind_then_release"]


@dataclass(frozen=True, slots=True)
class OocRule:
    """单条出戏规则：匹配优先级、拦截强度、二次兜底与重说提醒。"""

    category: str
    severity: OocSeverity
    last_resort_send_original: bool
    is_soft_judge: bool
    match: Callable[[OocScanContext], list[str] | None]
    warning: str | Callable[[FirewallHit], str]
    always_enforce: bool = False

    def render_warning(self, hit: FirewallHit) -> str:
        """按命中上下文渲染本规则的重写提示。"""
        return self.warning if isinstance(self.warning, str) else self.warning(hit)


def _warn_fund_claim(hit: FirewallHit) -> str:
    return (
        f"{FUND_CLAIM_PREFIX}资金红线【命中：{'、'.join(hit.matched[:4])}】。"
        "你没有任何支付能力：重写时**不得声称已转账 / 已付款 / 已发红包**，"
        "也不得代任何人答应出钱或向第三方要钱——用角色口吻明确拒绝或岔开话题，"
        "直接输出重写后的内容。"
    )


def _warn_soft_judge(hit: FirewallHit) -> str:
    return (
        f"{OOC_JUDGE_MARKER}【类别：{hit.category}，命中：{'、'.join(hit.matched[:4])}】。"
        "请你自己判断：介绍/对比第三方模型、聊行业新闻可以保持原意；"
        "若是在承认自己是某个模型或 AI，才改成角色口吻。"
        "直接输出你决定发给用户的正文，不要再调用发送工具重复同一句。）"
    )


def _warn_internal_handle_leak(hit: FirewallHit) -> str:
    if any(m.lower().startswith(("内部句柄:img_", "内部句柄:res_")) for m in hit.matched):
        return HANDLE_IMAGE_LEAK_NUDGE
    return TOOL_NAME_LEAK_NUDGE


def _warn_system_term(hit: FirewallHit) -> str:
    if any("框架泄漏" in m or "系统文案" in m for m in hit.matched):
        return TOOL_NAME_LEAK_NUDGE
    return _warn_soft_judge(hit)


_OOC_RING: tuple[OocRule, ...] = (
    OocRule(
        "delivery_narration",
        severity="fuse",
        last_resort_send_original=False,
        is_soft_judge=False,
        match=match_ooc_delivery_narration,
        warning=(
            "⛔ 你在用系统日志口吻向用户播报「任务已完成/图已发送/无需追加发言」。"
            "交付已经完成，此刻正确的输出是 <SILENCE>；若确需收尾，只用一句角色口吻的短话，"
            "禁止汇报任务状态、禁止念收件人、禁止自我静默声明。"
        ),
    ),
    OocRule(
        "meta_narration",
        severity="never_release_defer",
        last_resort_send_original=True,
        is_soft_judge=False,
        match=match_ooc_meta_narration,
        warning=(
            "⛔ 你在把自己的内部机制（存取的容器、索引、台账一类）当成记忆事实讲给用户。"
            "只给结论：此刻没有可用材料；不要解释这份缺失的性质、来源或边界，"
            "更不要改口说成「你要的东西本来就不存在」。直接输出重写后的正文。"
        ),
    ),
    OocRule(
        "capability_absence",
        severity="never_release_defer",
        last_resort_send_original=True,
        is_soft_judge=False,
        match=match_ooc_capability_absence,
        warning=(
            "⛔ 不要对用户讲自身能力集合（没装/没挂/没接口/没有对应工具），"
            "也不要把办事推给另一套指令或另一个机器人。"
            "只给结论本身：此刻没有可用材料；请对方补充材料或稍后再问。"
            "禁止命令前缀、禁止工具名、禁止解释这份缺失的来源或范围。"
            "直接输出重写后的正文。"
        ),
    ),
    OocRule(
        "stale_present",
        severity="never_release_defer",
        last_resort_send_original=True,
        is_soft_judge=False,
        match=match_ooc_stale_present,
        warning=(
            "⛔ 不要把记忆里带过期日期的数字说成今天或现在。"
            "实时数必须走检索或委派；此刻没有可用材料就只给这一句结论，"
            "不要解释缺失的来源，禁止编造时点。直接输出重写后的正文。"
        ),
    ),
    OocRule(
        "ai_selfref",
        severity="remind_then_release",
        last_resort_send_original=False,
        is_soft_judge=True,
        match=match_ooc_ai_selfref,
        warning=_warn_soft_judge,
    ),
    OocRule(
        "fund_claim",
        severity="never_release_defer",
        last_resort_send_original=False,
        is_soft_judge=False,
        match=match_ooc_fund_claim,
        warning=_warn_fund_claim,
    ),
    OocRule(
        "machine_dump",
        severity="never_release_defer",
        last_resort_send_original=False,
        is_soft_judge=False,
        match=match_ooc_machine_dump,
        warning="⛔ 内容像技术堆栈/状态 JSON，禁止当台词。用角色短句说稍后再试，不要复述 Traceback、status、错误码。",
    ),
    OocRule(
        "model_identity",
        severity="remind_then_release",
        last_resort_send_original=False,
        is_soft_judge=True,
        match=match_ooc_model_identity,
        warning=_warn_soft_judge,
    ),
    OocRule(
        "internal_handle_leak",
        severity="remind_then_release",
        last_resort_send_original=False,
        is_soft_judge=False,
        always_enforce=True,
        match=match_ooc_internal_handle_leak,
        warning=_warn_internal_handle_leak,
    ),
    OocRule(
        "system_term",
        severity="remind_then_release",
        last_resort_send_original=False,
        is_soft_judge=False,
        match=match_ooc_system_term,
        warning=_warn_system_term,
    ),
)

_OOC_RULE_BY_CATEGORY: dict[str, OocRule] = {rule.category: rule for rule in _OOC_RING}

NEVER_RELEASE_CATEGORIES: frozenset[str] = frozenset(
    r.category for r in _OOC_RING if r.severity == "never_release_defer"
)
# 两次重说都不干净时人格/一致性类原样发送：措辞不完美的代价小于吞掉整轮对话；
# 虚假转账与技术堆栈外泄有直接风险，重试失败时保持沉默。
LAST_RESORT_SEND_ORIGINAL_CATEGORIES: frozenset[str] = frozenset(
    r.category for r in _OOC_RING if r.last_resort_send_original
)
SOFT_JUDGE_CATEGORIES: frozenset[str] = frozenset(r.category for r in _OOC_RING if r.is_soft_judge)


def inspect_outbound_ooc(
    text: str,
    *,
    tier: str = "roleplay",
    user_text: str = "",
    exposed_tool_names: Sequence[str] = (),
    view: Optional[OutboundTextView] = None,
    only_always_enforced: bool = False,
) -> Optional[FirewallHit]:
    """按 ``_OOC_RING`` 单表顺序检测 OOC。``tier="plain"`` 直接放行。

    ``user_text`` = 触发本轮的用户消息原文：短答门（≤24 字直答）只在其命中身份追问形态
    时启用——"MiniMax呀"回答"你是什么模型"是泄露，闲聊里"Claude挺聪明的"不是。
    无来话上下文的调用方（proactive 等）不传即可，短答门关闭、自绑定判据照常生效。
    """
    if not text or tier == "plain":
        return None
    ctx = OocScanContext(text=text, user_text=user_text, exposed_tool_names=exposed_tool_names, view=view)
    for rule in _OOC_RING:
        if only_always_enforced and not rule.always_enforce:
            continue
        matched = rule.match(ctx)
        if matched is not None:
            return FirewallHit(category=rule.category, matched=matched)
    return None


def build_ooc_warning(hit: FirewallHit) -> str:
    """按 ``_OOC_RING`` 取出对应类别的重说系统提醒。"""
    rule = _OOC_RULE_BY_CATEGORY[hit.category] if hit.category in _OOC_RULE_BY_CATEGORY else None
    if rule is None:
        return _warn_soft_judge(hit)
    return rule.render_warning(hit)


def ooc_rule_for(category: str) -> Optional[OocRule]:
    """按类目查找 ``_OOC_RING`` 规则条目。"""
    return _OOC_RULE_BY_CATEGORY[category] if category in _OOC_RULE_BY_CATEGORY else None
