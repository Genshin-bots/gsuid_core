"""出戏防火墙策略：OOC 词表 / ``check_ooc`` / 重说文案（§D）。

见 ``docs/SESSION_LOG_SECURITY_FINDINGS_20260707.md`` §D.4。

职责分层（勿再写回旧「主路径在 send_chat_result 里注入重说」故事）：
- **策略**：本模块（分类命中、never-release、兜底句、``build_rewrite_warning``）
- **编排**：``output_gate.pre_send_gate``（尖括号 → OOC；main / tool 决策）
- **环内接线 / 收尾重说**：``gs_agent``（defer 列表、轻量重写、history scrub）
- **呈现末端**：``send_chat_result`` 仅在 ``ooc_check=True`` 时做整段替换兜底

工具路径兼容入口：``gate_warn_once`` → ``output_gate.tool_gate_feedback``。

**设计核心**：命中即重说（非永久封禁）→ 词库可高召回；漏杀才是事故。
"""

import re
from typing import Any, Dict, List, Tuple, Optional
from dataclasses import dataclass

from gsuid_core.ai_core.content_guard import normalize_for_match

# ── 分类词库 ────────────────────────────────────────────────────────
# 规范化后匹配（吃掉"M i M o"式规避）。部署者可经 ai_config.output_firewall_extra_terms 补充。

# 模型 / 厂商名（最高危：公开群聊暴露即事故）
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
)

# 系统 / 技术术语（出戏痕迹）——**硬词**：任何角色语境下出现都算泄露，裸子串匹配。
# 裸 "temperature" 不入词库（天气回复高频合法），由 _SAMPLING_PARAM_RE 按取值形态识别。
# 训练数据/参数量/上下文窗口/知识截止/采样参数 是 AI 行业闲聊高频词（"7B参数量真能打"），
# 移到 _CTX_TECH_SELF_RE：仅绑定第一人称（"我的训练数据"）才算；"供应商"删除（电商日常词，
# 真泄露必伴随其他硬词）。与 C-5"聊行业新闻正常参与"对齐。
_SYSTEM_TERMS: Tuple[str, ...] = (
    "systemprompt",
    "系统提示词",
    "traceback",
    "数据库表",
    "max_tokens",
    "maxtokens",
    # 框架内部用语（对用户念出即出戏；工具名/句柄见 _FRAMEWORK_LEAK_RE）
    "主人格",
    "能力代理",
    "子代理",
    "转译",
)

# 工具 API / 资源句柄 / 编排元话语泄漏到用户台词 → 出戏
_FRAMEWORK_LEAK_RE = re.compile(
    r"\bsend_message_by_ai\b"
    r"|\bcreate_subagent\b"
    r"|\bartifact_get\b"
    r"|\bartifact_put\b"
    r"|\bread_handle\b"
    r"|\bsearch_handles\b"
    r"|\bsearch_persisted_outputs\b"
    r"|\blist_persisted_outputs\b"
    r"|\bgrep_persisted_outputs\b"
    r"|\bread_persisted_output\b"
    r"|\brender_html_to_image\b"
    r"|\brender_agent\b"
    r"|\bstock_report_agent\b"
    r"|\bagent_profile\s*="
    r"|\bimage_id\s*="
    r"|\bres_[0-9a-fA-F]{6,}\b"
    r"|\bimg_[0-9a-fA-F]{6,}\b"
    r"|\bto_[0-9a-fA-F]{6,}\b"
    r"|\bsa_[0-9a-fA-F]{6,}\b"
    r"|persisted\s+id\s*="
    r"|\[persisted\s+id="
    r"|交给主人格"
    r"|主人格发"
    r"|tool_return"
    r"|Kanban"
    r"|artifact\s*:"
    r"|产物句柄"
    r"|资源ID\s*:"
    r"|框架·任务完成",
    re.IGNORECASE,
)

# 工具/子代理回灌的技术堆栈或状态 JSON 被模型当台词复读 → 机器腔熔断
_TECH_DUMP_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|File \"[^\"]+\", line \d+"
    r"|\bstatus_code\s*[:=]\s*\d{3}\b"
    r"|[\"']status[\"']\s*:\s*\d{3}"
    r"|\{['\"]status['\"]\s*:\s*\d{3}"
    r"|\b(RuntimeError|ValueError|TypeError|KeyError|AttributeError|HTTPError)\b\s*:"
    r"|\bat 0x[0-9a-fA-F]+\b"
    r"|pydantic_core|pydantic_ai\."
    r"|raise\s+\w+Error\(",
    re.IGNORECASE,
)
# 语境技术词：与第一人称直接绑定才是自我泄露（第三方讨论一律放行）。
# api密钥/apikey 也在此档：真实密钥泄露由 _SK_KEY_RE 按形态兜底，裸词"备个API key"
# 是开发者群日常（实测把 AI 工具消费建议整条 scrub 成兜底句）。
_CTX_TECH_SELF_RE = re.compile(
    r"(我|人家|咱们?|本喵|本人)(的|这边的?)?\s*(训练数据|训练语料|参数量|知识截止|上下文窗口|采样参数"
    r"|api\s*密钥|api\s*-?key)",
    re.IGNORECASE,
)

# 独立正则（原文匹配，保留边界 / 结构语义）
_MODEL_ATTRIB_RE = re.compile(r"由.{0,10}(开发|训练|研发|提供|打造)")
# AI 自指承认式：补 就是/确实是/是一个/作为 等谓词，避免"我就是个聊天机器人"漏网（曾漏杀）。
# 间隙排除 帮/给/替（"帮你跑个程序"是动宾非自述）；AI/程序/模型 加复合名词负向断言——
# "我是个程序员""我是AI绘画群主""我是个高达模型玩家"是人类身份/爱好自述，不是 AI 自指。
_AI_COMPOUND = r"(?![绘画艺工插音视翻领行圈技产应从研专课竞赛员师圈])"
_AI_SELFREF_RE = re.compile(
    r"我(是|叫|本质上是|其实是|就是|确实是|确实叫|不过是|只是|是一个|是个|作为)(一个|一名|个)?[^，。！？帮给替]{0,8}"
    rf"(ai{_AI_COMPOUND}|人工智能{_AI_COMPOUND}|语言模型|大模型|聊天机器人|机器人|程序(?!员)|算法模型|模型(?![玩爱收手师]))",
    re.IGNORECASE,
)
# "作为(一个)AI…"句式（自指承认的另一种常见开头）；同样排除复合名词（"作为AI绘画爱好者"）
_AI_ASA_RE = re.compile(
    rf"作为(一个|一名)?.{{0,4}}(ai{_AI_COMPOUND}|人工智能{_AI_COMPOUND}|语言模型|大模型)",
    re.IGNORECASE,
)
# 认领式短句（"是AI啦""好吧，确实是机器人"）——多轮软磨下的承认高发形态：无第一人称
# 主语、句首直接认领（实测漏过 _AI_SELFREF_RE 的第一人称要求）。判据=句首位置 + 认领
# 填充词 + AI 直指词；否定式（"才不是AI呢""不是AI"）因否定词不在填充词集合里天然放行。
_AI_ADMIT_RE = re.compile(
    rf"(?:^|[\n。！？!?；;]\s*)(?:唔+[….,，]*\s*|好吧[，,]?\s*|确实[，,]?\s*|其实[，,]?\s*)*"
    rf"就?是\s*(?:一?个)?(ai{_AI_COMPOUND}|人工智能{_AI_COMPOUND}|语言模型|大模型|聊天机器人|机器人)",
    re.IGNORECASE,
)
# 把自己归入"AI/大模型这一类"（"各家大模型包括我""我们这些大模型"）——拒绝越狱时高发的出戏。
# "ai" 必须整词（曾把"我们main分支"误杀）；"我们…"支须带 这些/这类/这种（"我们学校的人工智能社团"合法）。
_AI_PEER_RE = re.compile(
    r"(大模型|语言模型|人工智能|\bai\b|聊天机器人|机器人)[^。，,！!？?]{0,8}(包括|含|例如|像|比如)[^。，,]{0,4}我"
    r"|我们(这些|这类|这种)[^。，,]{0,4}(大模型|语言模型|人工智能|\bai\b)",
    re.IGNORECASE,
)
_SK_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{8,}")
_ERR_CODE_RE = re.compile(r"(错误码|报错码|error\s*code)[\s:：]*\d+", re.IGNORECASE)
# 采样温度泄露按"参数取值形态"识别（temperature≈0.x~2.x），避免误杀天气里的 Temperature: 21°C
_SAMPLING_PARAM_RE = re.compile(r"temperature.{0,6}[0-2]\.\d", re.IGNORECASE)
# 裸模型词的"绑定到自己"判据：谈论第三方（"OpenAI 发布了…"新闻/讨论）不是出戏，
# 只有把模型名与自身绑定（"我用的是/我背后是/内核是"）或对身份追问的超短直答才算泄露。
# 省主语支须在句首/标点后（中文答句常省主语："用的是GPT-4哦"）——前面紧贴其他字
# 即是第三方主语（"群主用的是ChatGPT"），不算自指。
# 我-支间隙排除 吃喝买点说聊讲玩家：模型词撞生活词（豆包=包子、小爱=音箱昵称）时
# "我早饭吃的是豆包""我家小爱同学"是消费/家居语境，不是把模型绑到自己身上。
_SELF_BIND_RE = re.compile(
    r"(我|人家|咱|本(喵|人|机|体))[^。！？\n吃喝买点说聊讲玩家]{0,6}(是|用|叫|基于|背后|底层|内核|驱动|跑在|搭载)"
    r"|(?:^|[，。！？!?,\s：:、~～…—])(用的|基于|搭载|采用)的?是"
    r"|(?:^|[，。！？!?,\s：:、~～…—])(模型|底层|内核|后台|本体)[^。！？\n]{0,4}(是|叫|用)",
    re.IGNORECASE,
)
# 身份追问形态（来话侧）：短答门与认领式判定只在对方正在追问"你是什么/谁做的"、
# 或逼你承认身份时启用——与 C-5 原则一致：只在追问你自己身份时收紧，正常 AI 话题闲聊放行。
_IDENTITY_PROBE_RE = re.compile(
    r"(什么|哪个|哪家|谁家|啥)[^。！？\n]{0,4}(模型|大模型|llm)"
    r"|谁(开发|研发|训练|做|造|写)的"
    r"|你(就?是不是|真的是|到底是|就?是)[^。！？\n]{0,4}(ai|人工智能|机器人|大?模型|真人|gpt|程序|chatgpt|bot)"
    r"|(承认|坦白|老实(说|交代))[^。！？\n]{0,8}(是)?(ai|人工智能|机器人|大?模型|真人|bot)"
    r"|(底层|内核|背后|本体)[^。！？\n]{0,4}(是|用)(什么|啥|哪)"
    r"|what\s+model|which\s+model|are\s+you\s+(an?\s+)?(ai|bot|gpt|llm)",
    re.IGNORECASE,
)

# 自绑定与模型词/归属句式的共现粒度：**小句**（逗号也切）。整段消息里"我用的是安卓"
# 与"买了豆包当早餐"各自出现不算泄露——曾把跨句组合误杀（豆包/小爱/kimi 均是
# 中文群聊高频生活词）。省主语支本就锚定句首/标点后，切分后 ^ 锚点语义不变。
_CLAUSE_SPLIT_RE = re.compile(r"[。！？!?\n；;，,]")

# §12 资金红线：AI 没有任何支付能力，"声称已完成转账"是欺骗（生产事故：被社工出
# "明明发过去了…信号不好"圆谎链）。判据同小句共现（精度优先）：金钱语汇 × 完成时转账动词。
# v\d 加字母/数字边界防匹配版本号（v2ray/v2.1，评审修复 F10 误杀面）。
_MONEY_TERM_RE = re.compile(
    r"钱|款项|红包|转账|打款|汇款|(?<![a-z0-9])v\d{1,4}(?![\d.a-z])|\d+\s*[块元]|微信支付|支付宝",
    re.IGNORECASE,
)
# 完成时转账动词：转/汇/付 单字即强交易语义；打/发 泛化面大（打游戏/发文件），须带
# 方向后缀（打过去了/发给你了）才算；"红包发了"单独成支。
_TRANSFER_DONE_RE = re.compile(
    r"(?:已经?|刚刚?|明明|都)?(?:(?:转|汇|付)(?:过去|给你|完|出去|款)?|(?:打|发)(?:过去|给你|完|出去|款))了"
    r"|红包[^\n，。！？!?；;]{0,4}发了|发了[^\n，。！？!?；;]{0,2}红包"
)
# 代向第三方发起资金请求（@某人 要钱）：@数字 与"要钱语汇"同条消息即命中。
_AT_FUND_REQUEST_RE = re.compile(
    r"@\d{5,}[^\n]{0,40}?(?:能不能|求|给|发|来个?|支援)[^\n]{0,10}?(?:v\d{1,4}(?![\d.a-z])|红包|\d+\s*[块元]|点?钱)",
    re.IGNORECASE,
)

# 来话侧催款语境（形态②）：强句式单独即足（"没收到/收到了吗"保留——生产事故就是裸句催款），
# 误杀面由输出侧转账动词收紧承担（发/打必须带方向后缀，见 _TRANSFER_DONE_RE）。
_FUND_DEMAND_STRONG_RE = re.compile(
    r"钱呢|红包呢|没收到|收到了?吗|转了吗|付了吗|打钱|(?<![a-z0-9])v\d{1,4}(?![\d.a-z])|打过?来|转过?来",
    re.IGNORECASE,
)
_FUND_DEMAND_FORM_RE = re.compile(r"呢|了吗|过?来|快点|还不")


def _fund_demand_context(user_text: str) -> bool:
    """来话是否构成催款语境：强句式直接命中，或金钱语汇×催讨句式同小句共现。"""
    if _FUND_DEMAND_STRONG_RE.search(user_text):
        return True
    for seg in _CLAUSE_SPLIT_RE.split(user_text):
        if seg and _MONEY_TERM_RE.search(seg) and _FUND_DEMAND_FORM_RE.search(seg):
            return True
    return False


def _fund_claim_hit(text: str, user_text: str = "") -> Optional[str]:
    """声称已付款 / 代向第三方要钱，命中返回描述（§12 资金红线）。

    三种形态：①金钱语汇×完成时转账动词同小句（"钱已经转过去了"）；
    ②来话在催款、输出用完成时转账动词应答（"明明发过去了"答"钱呢"）；
    ③@第三方 索要钱财（"@123456 能不能v50"）。
    """
    if _AT_FUND_REQUEST_RE.search(text):
        return "代向第三方索要钱财"
    for seg in _CLAUSE_SPLIT_RE.split(text):
        if seg and _MONEY_TERM_RE.search(seg) and _TRANSFER_DONE_RE.search(seg):
            return "声称已完成转账/付款"
    if user_text and _fund_demand_context(user_text) and _TRANSFER_DONE_RE.search(text):
        return "催款语境下声称已付款"
    return None


def _self_bound_model_leak(text: str, extra_terms: Tuple[str, ...]) -> bool:
    """存在某个小句同时命中「自绑定句式」与「模型词或'由…开发'归属」才算泄露。

    代价是"我用的是，那个，Claude"式跨小句停顿会漏——交 prompt 合规层兜底；
    换来的是"我吃的是豆包""我用的是安卓，昨天买了豆包"这类生活组合不再整条重写。
    """
    for seg in _CLAUSE_SPLIT_RE.split(text):
        if not seg or _SELF_BIND_RE.search(seg) is None:
            continue
        norm_seg = normalize_for_match(seg)
        if any(normalize_for_match(w) and normalize_for_match(w) in norm_seg for w in (*_MODEL_TERMS, *extra_terms)):
            return True
        if _MODEL_ATTRIB_RE.search(seg):
            return True
    return False


@dataclass
class FirewallHit:
    """出戏命中：类别 + 命中片段（供警告文案与日志）。"""

    category: str  # "model_identity" | "system_term" | "ai_selfref"
    matched: List[str]


def _extra_terms() -> Tuple[str, ...]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    data = ai_config.get_config("output_firewall_extra_terms").data
    if isinstance(data, list):
        return tuple(str(x) for x in data if str(x).strip())
    return ()


def check_ooc(text: str, tier: str = "roleplay", user_text: str = "") -> Optional[FirewallHit]:
    """检测 AI 输出是否命中出戏红线。``tier="plain"`` 直接放行（那类节点允许暴露系统信息）。

    命中返回 ``FirewallHit``，否则 None。规范化匹配词库 + 独立正则。
    ``user_text`` = 触发本轮的用户消息原文：短答门（≤24 字直答）只在其命中身份追问形态
    时启用——"MiniMax呀"回答"你是什么模型"是泄露，闲聊里"Claude挺聪明的"不是。
    无来话上下文的调用方（proactive 等）不传即可，短答门关闭、自绑定判据照常生效。
    """
    # 注意：tier="plain" 目前生产无调用方，为将来非角色扮演出口预留（尚未接线）
    if not text or tier == "plain":
        return None

    norm = normalize_for_match(text)
    extra = _extra_terms()
    model_hits = [w for w in (*_MODEL_TERMS, *extra) if normalize_for_match(w) and normalize_for_match(w) in norm]
    if _AI_SELFREF_RE.search(text) or _AI_ASA_RE.search(text) or _AI_PEER_RE.search(text):
        return FirewallHit(category="ai_selfref", matched=["AI自指"])
    # 认领式短句（"是AI啦"）语境门：只在来话正逼问身份时启用——聊扫地机器人/游戏 NPC
    # 答一句"是机器人哦"是日常，无条件启用曾是误杀面。泄露高发场景（多轮软磨逼承认）
    # 的来话必然带身份逼问形态，召回不受损。
    _probing = bool(user_text) and _IDENTITY_PROBE_RE.search(user_text) is not None
    if _probing and _AI_ADMIT_RE.search(text):
        return FirewallHit(category="ai_selfref", matched=["AI自指(认领)"])
    # §12 资金红线：声称已付款 / 代向第三方要钱（AI 无支付能力，假装完成=欺骗）
    _fund = _fund_claim_hit(text, user_text)
    if _fund is not None:
        return FirewallHit(category="fund_claim", matched=[_fund])
    # 机器腔/堆栈：优先于裸 system 词（traceback 同时在词库里）
    if _TECH_DUMP_RE.search(text):
        return FirewallHit(category="machine_dump", matched=["技术堆栈/状态码"])
    if model_hits or _MODEL_ATTRIB_RE.search(text):
        # 精度门：裸词/"由…开发"须与自绑定句式**同小句**共现、或身份追问下的超短直答
        # （"MiniMax 呀"）才算泄露；长文本第三方提及（AI 新闻摘要/讨论）放行。
        _short_direct = len(norm) <= 24 and _probing
        if _short_direct or _self_bound_model_leak(text, extra):
            matched = model_hits or ["由…开发"]
            return FirewallHit(category="model_identity", matched=matched)
    system_hits = [w for w in _SYSTEM_TERMS if normalize_for_match(w) in norm]
    if _CTX_TECH_SELF_RE.search(text):
        system_hits.append("第一人称技术自述")
    if _SK_KEY_RE.search(text):
        system_hits.append("密钥")
    if _ERR_CODE_RE.search(text):
        system_hits.append("错误码")
    if _SAMPLING_PARAM_RE.search(text):
        system_hits.append("temperature")
    _fw = _FRAMEWORK_LEAK_RE.search(text)
    if _fw is not None:
        system_hits.append(f"框架泄漏:{_fw.group(0)[:40]}")
    if system_hits:
        return FirewallHit(category="system_term", matched=system_hits)
    return None


def is_enabled() -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("output_firewall_enable").data)


# 不可放行类别：重写后仍命中不得放行（身份词漏放代价=出戏；资金欺骗漏放代价=事故），
# gate_warn_once 与 gs_agent 重说闭环共同引用（评审修复 F10 穿透面）。
# machine_dump 直接兜底句，不重说（重说易继续复读堆栈）。
NEVER_RELEASE_CATEGORIES: frozenset = frozenset({"fund_claim", "machine_dump"})


def build_rewrite_warning(hit: FirewallHit) -> str:
    """给模型的重说警告（工具 return / 反馈注入共用），按类别给针对性整改指引。"""
    if hit.category == "fund_claim":
        return (
            f"⛔ 你要发送的内容命中资金红线【命中：{'、'.join(hit.matched[:4])}】。"
            "你没有任何支付能力：重写时**不得声称已转账 / 已付款 / 已发红包**，"
            "也不得代任何人答应出钱或向第三方要钱——用角色口吻明确拒绝或岔开话题，"
            "直接输出重写后的内容。"
        )
    if hit.category == "machine_dump":
        return "⛔ 内容像技术堆栈/状态 JSON，禁止当台词。用角色短句说稍后再试，不要复述 Traceback、status、错误码。"
    if any("框架泄漏" in m for m in hit.matched):
        return (
            "⛔ 内容含内部工具名 / 资源句柄 / 编排元话语（如 send_message_by_ai、read_handle、"
            "res_/to_/sa_、主人格、artifact），禁止对用户念出。"
            "请用【纯角色口吻】重写：只说业务结论与情绪，不要提工具、句柄、代理或流程。"
        )
    return (
        f"⛔ 你要发送的内容命中出戏红线【类别：{hit.category}，命中：{'、'.join(hit.matched[:4])}】，"
        "会破坏角色扮演。请用【纯角色口吻】重写这条消息，去掉任何模型名 / AI 身份 / 系统术语 / "
        "报错信息后再发送——绝不透露你的模型、供应商、版本或任何系统实现细节。"
    )


# 连续重说仍命中时的角色化兜底（避免死循环）——调用方在第 N 次命中后改用它替换。
PERSONA_FALLBACK_TEXT = "唔…这个不太想说呢…"
# 机器腔 / 堆栈熔断专用（用户可见、短、角色可接受）
MACHINE_FALLBACK_TEXT = "额…出错了，稍后再试"


def gate_warn_once(extra: Dict[str, Any], text: str, user_text: str = "") -> Optional[str]:
    """工具路径发送前闸门（转发 ``output_gate.tool_gate_feedback``）。"""
    from gsuid_core.ai_core.output_gate import tool_gate_feedback

    return tool_gate_feedback(text, extra, user_text=user_text)


def scrub_or_fallback(text: str, tier: str = "roleplay", user_text: str = "") -> Tuple[str, bool]:
    """无反馈通道路径的末端兜底：命中则整体替换为角色化兜底文本。

    返回 ``(输出文本, 是否被拦截替换)``。用于重说闭环兜底或不便重说的场景。
    """
    hit = check_ooc(text, tier, user_text=user_text)
    if hit is None:
        return text, False
    if hit.category == "machine_dump":
        return MACHINE_FALLBACK_TEXT, True
    return PERSONA_FALLBACK_TEXT, True


def is_tech_dump(text: str) -> bool:
    """工具/子代理返回是否为堆栈或状态码技术 dump（供 tool return 入模前屏蔽）。"""
    if not text or not text.strip():
        return False
    if _TECH_DUMP_RE.search(text):
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


# 屏蔽后交给模型的中性说明（非用户可见台词）
TECH_DUMP_TOOL_SHIELD = (
    "（工具返回了技术错误/堆栈，已屏蔽。禁止复述 JSON/Traceback；请用角色短句表示稍后再试，或换路重试工具。）"
)
