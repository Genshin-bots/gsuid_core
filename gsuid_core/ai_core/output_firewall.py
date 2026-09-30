"""出戏防火墙策略：OOC 词表 / ``check_ooc`` / 重说文案（§D）。

见 ``docs/SESSION_LOG_SECURITY_FINDINGS_20260707.md`` §D.4。

职责分层（勿再写回旧「主路径强制剥模型名 / 工具路径二次发送放行」故事）：
- **策略**：本模块（分类命中、never-release、``build_rewrite_warning``）
- **编排**：``output_gate.pre_send_gate``（尖括号 → OOC；main / tool 决策）
- **环内接线**：``gs_agent``（系统提醒注入、收尾自主判断、history scrub）
- **呈现末端**：``send_chat_result`` 仅在 ``ooc_check=True`` 时做整段丢弃

**本模块不再提供任何罐头兜底文本**：命中后一律由当前人格重说一句
（``gs_agent._ooc_recover_persona_voice``），两次都不干净时人格/一致性类按
``_LAST_RESORT_SEND_ORIGINAL`` 原样发送、``fund_claim`` / ``machine_dump`` 走沉默；
没有 run 的出口（主动播报）直接不播报。框架不替人格说话。

工具路径兼容入口：``gate_warn_once`` → ``output_gate.tool_gate_feedback``。

**设计核心**：软出戏命中 = 系统提醒 + 模型自主判断（非强制改写 / 非二次放行）。
词库可高召回；资金 / 机器腔仍 never-release。
"""

import re
from typing import Any, Dict, List, Tuple, Optional, Sequence
from dataclasses import dataclass

from gsuid_core.ai_core.content_guard import normalize_for_match

# ── 分类词库 ────────────────────────────────────────────────────────
# 规范化后匹配（吃掉"M i M o"式规避）。部署者可经 ai_config.output_firewall_extra_terms 补充。

# 模型 / 厂商名（最高危：公开群聊暴露即事故）
# 规范化后是子串匹配：短码/颜文字/成语/生活词不收（qwq、xai、即梦、混元、可灵、元宝）。
# 部署者自家供应商走 ai_config.output_firewall_extra_terms，不要往这里硬编码。
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

# 系统 / 技术术语（出戏痕迹）——**硬词**：任何角色语境下出现都算泄露，裸子串匹配。
# 裸 temperature / traceback 不入词库（天气、代码评审高频合法），改由 _SAMPLING_PARAM_RE
# 与 _TECH_DUMP_RE 按取值形态识别。
# 训练数据/参数量/上下文窗口/知识截止/采样参数 是 AI 行业闲聊高频词（"7B参数量真能打"），
# 移到 _CTX_TECH_SELF_RE：仅绑定第一人称（"我的训练数据"）才算；"供应商"删除（电商日常词，
# 真泄露必伴随其他硬词）。与 C-5"聊行业新闻正常参与"对齐。
_SYSTEM_TERMS: Tuple[str, ...] = (
    "systemprompt",
    "系统提示词",
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
    r"|\bweb_search_tool\b"
    r"|\bweb_search\b"
    r"|\bfind_tools\b"
    r"|\brender_html_to_image\b"
    r"|\brender_agent\b"
    r"|\bresearch_agent\b"
    r"|\bagent_profile\s*="
    r"|\bimage_id\s*="
    r"|\bres_[0-9a-fA-F]{6,}\b"
    r"|\bimg_[0-9a-fA-F]{6,}\b"
    r"|\bto_[0-9a-fA-F]{6,}\b"
    r"|\bsa_[0-9a-fA-F]{6,}\b"
    r"|\bdlg_[0-9a-fA-F-]{8,}\b"
    r"|\bcheck_delegation\b"
    r"|\bdispute_directive\b"
    r"|persisted\s+id\s*="
    r"|\[persisted\s+id="
    r"|交给主人格"
    r"|主人格发"
    r"|tool_return"
    r"|long_structured"
    r"|inline_head"
    r"|how_to_read"
    r"|Kanban"
    r"|artifact\s*:"
    r"|产物句柄"
    r"|资源ID\s*:"
    r"|框架·任务完成"
    r"|系统校验",
    re.IGNORECASE,
)

# 系统过程文案 / 内部口头禅对用户泄露（gateway 硬拦）
_SYSTEM_COPY_LEAK_RE = re.compile(
    r"(时效存疑|自己再验|数据没刷|没刷出来|没法.{0,8}编数字|"
    r"回炉了?你再|回炉|"
    r"（系统提示|（系统校验|\[框架[·・.]|"
    r"禁止再检索|禁止把句柄|禁止念|"
    r"create_subagent\(|agent_profile=)",
    re.IGNORECASE,
)

# 工具/子代理回灌的技术堆栈或状态 JSON 被模型当台词复读 → 机器腔熔断
# 状态码只认 4xx/5xx：2xx 是成功态，"接口 status_code 返回 200" 是运维/代码评审日常，
# 按 \d{3} 无差别拦会把整条 scrub 成兜底句（与裸 traceback 同类的误杀面）。
_TECH_DUMP_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|File \"[^\"]+\", line \d+"
    r"|\bstatus_code\s*[:=]\s*[45]\d\d\b"
    r"|[\"']status[\"']\s*:\s*[45]\d\d"
    r"|\{['\"]status['\"]\s*:\s*[45]\d\d"
    r"|\bat 0x[0-9a-fA-F]+\b"
    r"|pydantic_core|pydantic_ai\.",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
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
_SK_KEY_RE = re.compile(r"(?<![A-Za-z])sk-[A-Za-z0-9]{8,}")
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
    r"@\d{5,}[^\n]{0,40}?(?:能不能|求|给|发|来个|支援)[^\n]{0,10}?(?:v\d{1,4}(?![\d.a-z])|红包|\d+\s*(?:块钱|元钱)|点?钱)",
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


# 过程元叙述：把自身检索机制当记忆事实讲给用户（实测「内部库没你的分值」）。
# 判据是**同一小句**三信号共现而非词表：自指内部机制 × 机制名词 × 缺失谓词。
_META_SELF_MECH_RE = re.compile(
    r"(内部|后台|服务器|数据库|引擎|系统)"
    r"|(我|咱|俺|人家|本人|本喵|本机)[^。！？\n，,；;]{0,6}(这边|这头|底下|手里|手头|手上|这儿)"
)
# 「库」必须带机制限定词：库存/书库/粮库/车库是日常名词，不算内部机制。
_MECH_NOUN = r"(?:内部|后台|资料|知识|记忆|数据|私有|本地|检索|素材|云端)库|数据表|检索层|检索器|词表|提示词|数据源"
# meta_narration 已额外要求自指内部机制，故容许「记录/索引/缓存」这类偏泛的容器名。
_META_MECH_NOUN = _MECH_NOUN + r"|记录|档案|台账|索引|条目|日志|缓存|上下文|数据"
_MECH_NOUN_RE = re.compile(_MECH_NOUN)
_META_MECH_NOUN_RE = re.compile(_META_MECH_NOUN)
# 「没有」不能当独立词条：它是「没+有」的组合，"没有问题""没有异常"里都含这个子串。
_ABSENT_TERM = r"(没(?!有?(?:问题|异常|关系|事|必要|意思|兴趣|毛病))|未(?!来)|不存在|尚未|是空)"
# 缺失谓词须贴住机制名词（不许裸「不/没」），避免"上下文无上限"这类正常句被吞。
_MECH_ABSENT_RE = re.compile(
    rf"(?:{_MECH_NOUN})[^。！？\n，,；;]{{0,6}}{_ABSENT_TERM}"
    rf"|{_ABSENT_TERM}[^。！？\n，,；;]{{0,6}}(?:{_MECH_NOUN})"
)
# meta_narration 侧的否定词表比上面窄：**裸「不/别/甭」不能当缺失判据**，
# "不错""不好意思""无所谓"里的这些字都在正常词内部；只有贴住机制名词才判。
_META_ABSENT_TERM = (
    r"(?:没(?!有?(?:问题|异常|关系|事|必要|意思|兴趣|毛病))|未(?!来)|无(?!所谓|论|声|法)"
    r"|尚未|不存在|并非|并不|是空"
    # 「不」只留"不完整"族：裸「不有/不到/不着」会命中不有趣、不到一百行、不着急；
    # 全/留 还要排掉「不全是错的」「不留情」这类固定搭配。
    r"|不(?:太|怎么|大)?(?:全(?!是|都|对)|完整|准(?!备)|精确|齐|新鲜|覆盖|留(?!情)|保存|存(?!放)|记得)"
    r"|(?:查|搜|找|提|拿|取)不到|找不到|查不着)"
)
# 缺失谓词必须**同小句**且贴住机制名词。跨逗号桥接（「系统日志不错，没吃饭」会被
# 判成机制缺失）代价太大：中文里否定是否属于前一小句的对象，分词层面不可判。
_META_MECH_ABSENT_RE = re.compile(
    rf"(?:{_META_MECH_NOUN})[^。！？\n，,；;]{{0,6}}(?:{_META_ABSENT_TERM})"
    rf"|(?:{_META_ABSENT_TERM})[^。！？\n，,；;]{{0,6}}(?:{_META_MECH_NOUN})"
)


def looks_like_meta_narration(text: str) -> bool:
    """台词是否把自身检索机制当成记忆事实讲出去（meta_narration）。

    三个信号必须**同一小句**（逗号也切）共现：自指内部机制 × 机制名词 × 缺失谓词。
    任一信号单独出现都是正常人话（"我翻了翻你的角色箱""记录我记着呢"）。
    机制名词走规范化形态，词内插空格的规避写法因此仍按同句共现判。
    """
    for seg in _CLAUSE_SPLIT_RE.split(text):
        if not seg or _META_SELF_MECH_RE.search(seg) is None:
            continue
        norm_seg = normalize_for_match(seg)
        if _META_MECH_NOUN_RE.search(norm_seg) and _META_MECH_ABSENT_RE.search(norm_seg):
            return True
    return False


def _mechanism_absence_clause(text: str) -> bool:
    """机制名词 + 缺失谓词同小句（``capability_absence`` 扩容用）。

    ``speech_policy`` 那侧只认「工具/接口」字面量，"内部库没你的分值"这类
    不带这两个字的机制自述会漏；这里只补这一维，仍要求同小句、只收无歧义机制名。
    """
    for seg in _CLAUSE_SPLIT_RE.split(text):
        if seg and _MECH_NOUN_RE.search(normalize_for_match(seg)) and _MECH_ABSENT_RE.search(seg):
            return True
    return False


@dataclass
class FirewallHit:
    """出戏命中：类别 + 命中片段（供警告文案与日志）。"""

    category: str  # model_identity | system_term | ai_selfref | capability_absence | meta_narration
    matched: List[str]


def _extra_terms() -> Tuple[str, ...]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    data = ai_config.get_config("output_firewall_extra_terms").data
    if isinstance(data, list):
        return tuple(str(x) for x in data if str(x).strip())
    return ()


# ToolContext.extra 键：本轮已暴露的工具名集合（装配池 ∪ find_tools）。
EXPOSED_TOOLS_EXTRA_KEY = "exposed_tool_names"
_EXPOSED_TOOL_TOKEN_RE = re.compile(r"`([A-Za-z][A-Za-z0-9_]{2,})`|\b([A-Za-z][A-Za-z0-9_]{2,})\b")


def _exposed_tool_name_leak(text: str, names: Sequence[str]) -> str | None:
    """台词里是否出现本轮已暴露的工具标识符（词边界 / 反引号，全等）。"""
    if not text or not names:
        return None
    lower_map = {n.lower(): n for n in names if n}
    if not lower_map:
        return None
    for m in _EXPOSED_TOOL_TOKEN_RE.finditer(text):
        token = m.group(1) or m.group(2)
        if token is None:
            continue
        key = token.lower()
        if key in lower_map:
            return lower_map[key]
    return None


def check_ooc(
    text: str,
    tier: str = "roleplay",
    user_text: str = "",
    exposed_tool_names: Sequence[str] = (),
) -> Optional[FirewallHit]:
    """检测 AI 输出是否命中出戏红线。``tier="plain"`` 直接放行（那类节点允许暴露系统信息）。

    命中返回 ``FirewallHit``，否则 None。规范化匹配词库 + 独立正则。
    ``user_text`` = 触发本轮的用户消息原文：短答门（≤24 字直答）只在其命中身份追问形态
    时启用——"MiniMax呀"回答"你是什么模型"是泄露，闲聊里"Claude挺聪明的"不是。
    无来话上下文的调用方（proactive 等）不传即可，短答门关闭、自绑定判据照常生效。
    """
    # 注意：tier="plain" 目前生产无调用方，为将来非角色扮演出口预留（尚未接线）
    if not text or tier == "plain":
        return None

    # 交付状态汇报 / 能力缺失 / 过期时点：结构判定，优先于词库
    from gsuid_core.ai_core.agent_run.speech_policy import (
        looks_like_capability_absence,
        looks_like_stale_present_tense,
        looks_like_delivery_status_narration,
    )

    if looks_like_delivery_status_narration(text):
        return FirewallHit(category="delivery_narration", matched=["交付状态汇报"])
    if looks_like_meta_narration(text):
        return FirewallHit(category="meta_narration", matched=["内部机制自述"])
    if looks_like_capability_absence(text) or _mechanism_absence_clause(text):
        return FirewallHit(category="capability_absence", matched=["能力缺失叙述"])
    if looks_like_stale_present_tense(text):
        return FirewallHit(category="stale_present", matched=["过期时点当现在"])

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
    # 机器腔/堆栈：优先于裸 system 词
    if _TECH_DUMP_RE.search(_CODE_FENCE_RE.sub(" ", text)):
        return FirewallHit(category="machine_dump", matched=["技术堆栈/状态码"])
    _dev = _dev_vocab_hit(text)
    if _dev is not None:
        return FirewallHit(category="dev_vocab", matched=[_dev])
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
    _sc = _SYSTEM_COPY_LEAK_RE.search(text)
    if _sc is not None:
        system_hits.append(f"系统文案:{_sc.group(0)[:40]}")
    _tool_leak = _exposed_tool_name_leak(text, exposed_tool_names)
    if _tool_leak is not None:
        system_hits.append(f"框架泄漏:{_tool_leak}")
    if system_hits:
        return FirewallHit(category="system_term", matched=system_hits)
    if _objective_framework_intro(text):
        return FirewallHit(category="system_term", matched=["客观介绍宿主框架"])
    return None


_DEV_VOCAB_RE = re.compile(r"(工具(?!人)|接口|配置|服务).{0,12}(没配|没好|失败|报错|不可用|还没|未配置|配好)")
_DEV_VOCAB_WHITELIST_RE = re.compile(r"(数据口径|统计口径|工具人)")
_FRAMEWORK_TECH_RE = re.compile(r"(FastAPI|WebSocket|框架|插件系统|Python|SQLAlchemy)")
_FRAMEWORK_IS_RE = re.compile(r"是(一个)?")


def _dev_vocab_hit(text: str) -> Optional[str]:
    if _DEV_VOCAB_WHITELIST_RE.search(text):
        return None
    m = _DEV_VOCAB_RE.search(text)
    if m is None:
        return None
    return m.group(0)[:40]


def _framework_alias_list() -> list[str]:
    from gsuid_core.config import core_config

    raw = core_config.get_config("framework_aliases")
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw if str(x).strip()]
    return ["GsCore", "gsuid_core"]


def _objective_framework_intro(text: str) -> bool:
    """框架名 +「是」+ 技术名词的说明文。角色化转述不含该形态，不命中。"""
    if not _FRAMEWORK_IS_RE.search(text) or not _FRAMEWORK_TECH_RE.search(text):
        return False
    return any(alias in text for alias in _framework_alias_list() if alias)


def is_enabled() -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("output_firewall_enable").data)


# 资金欺骗 / 机器腔：提醒后仍不得放行。软出戏（身份词）走系统提醒 + 自主判断。
NEVER_RELEASE_CATEGORIES: frozenset[str] = frozenset(
    {"fund_claim", "machine_dump", "capability_absence", "meta_narration", "stale_present"}
)
#: 两次重说都不干净时**原样发送**的类目（人格 / 一致性类）。理由：原样放行的代价只是措辞
#: 不完美，而吞掉整轮的代价是用户什么都收不到——出戏闸不该吃掉一次正常对话。
_LAST_RESORT_SEND_ORIGINAL: frozenset[str] = frozenset({"capability_absence", "meta_narration", "stale_present"})
#: 不在上表里的两个（``fund_claim`` 虚假转账声明 / ``machine_dump`` 技术堆栈）：它们的
#: **原文本身就是闸门要防的东西**——原样发出去不是「不完美」而是「有害」（社工话术带偏
#: 的生产事故 / 内部堆栈外泄），故最后一档仍走沉默。模型主动回 ``<SILENCE>`` 同理。
SOFT_JUDGE_CATEGORIES: frozenset[str] = frozenset({"model_identity", "ai_selfref"})
OOC_JUDGE_MARKER = "（系统校验：刚才要发的内容可能出戏"


def build_rewrite_warning(hit: FirewallHit) -> str:
    """给模型的系统提醒（工具 return / 反馈注入共用）。软出戏只请模型自判，不强制改写。"""
    if hit.category == "fund_claim":
        return (
            f"⛔ 你要发送的内容命中资金红线【命中：{'、'.join(hit.matched[:4])}】。"
            "你没有任何支付能力：重写时**不得声称已转账 / 已付款 / 已发红包**，"
            "也不得代任何人答应出钱或向第三方要钱——用角色口吻明确拒绝或岔开话题，"
            "直接输出重写后的内容。"
        )
    if hit.category == "machine_dump":
        return "⛔ 内容像技术堆栈/状态 JSON，禁止当台词。用角色短句说稍后再试，不要复述 Traceback、status、错误码。"
    if hit.category == "delivery_narration":
        return (
            "⛔ 你在用系统日志口吻向用户播报「任务已完成/图已发送/无需追加发言」。"
            "交付已经完成，此刻正确的输出是 <SILENCE>；若确需收尾，只用一句角色口吻的短话，"
            "禁止汇报任务状态、禁止念收件人、禁止自我静默声明。"
        )
    if hit.category == "capability_absence":
        return (
            "⛔ 不要对用户讲自身能力集合（没装/没挂/没接口/没有对应工具），"
            "也不要把办事推给另一套指令或另一个机器人。"
            "只给结论本身：此刻没有可用材料；请对方补充材料或稍后再问。"
            "禁止命令前缀、禁止工具名、禁止解释这份缺失的来源或范围。"
            "直接输出重写后的正文。"
        )
    if hit.category == "meta_narration":
        return (
            "⛔ 你在把自己的内部机制（存取的容器、索引、台账一类）当成记忆事实讲给用户。"
            "只给结论：此刻没有可用材料；不要解释这份缺失的性质、来源或边界，"
            "更不要改口说成「你要的东西本来就不存在」。直接输出重写后的正文。"
        )
    if hit.category == "stale_present":
        return (
            "⛔ 不要把记忆里带过期日期的数字说成今天或现在。"
            "实时数必须走检索或委派；此刻没有可用材料就只给这一句结论，"
            "不要解释缺失的来源，禁止编造时点。直接输出重写后的正文。"
        )
    if any("框架泄漏" in m or "系统文案" in m for m in hit.matched):
        return (
            "（系统校验：内容可能含内部工具名 / 句柄 / 编排文案。"
            "请判断后用角色口吻只说结论；不要对用户念工具、句柄或内部提示。"
            "直接输出你决定发给用户的正文，不要再调用发送工具重复同一句。）"
        )
    return (
        f"{OOC_JUDGE_MARKER}【类别：{hit.category}，命中：{'、'.join(hit.matched[:4])}】。"
        "请你自己判断：介绍/对比第三方模型、聊行业新闻可以保持原意；"
        "若是在承认自己是某个模型或 AI，才改成角色口吻。"
        "直接输出你决定发给用户的正文，不要再调用发送工具重复同一句。）"
    )


# 这里曾经有 fallback_ooc_text / fallback_machine_text 两个罐头访问器
# （persona.json 的 fallback_ooc / fallback_machine），现已删除：连续重说仍命中时
# 没有罐头可退——永不放行类目走「人格再重说一句 → 仍不干净就沉默」
# （``GsCoreAIAgent._ooc_recover_persona_voice``），无 run 的出口直接丢弃正文。
# 禁抄任何人格口癖（AGENTS.md §1.9）。


def gate_warn_once(extra: Dict[str, Any], text: str, user_text: str = "") -> Optional[str]:
    """工具路径发送前闸门（转发 ``output_gate.tool_gate_feedback``）。"""
    from gsuid_core.ai_core.output_gate import tool_gate_feedback

    return tool_gate_feedback(text, extra, user_text=user_text)


def scrub_or_drop(
    text: str,
    tier: str = "roleplay",
    user_text: str = "",
) -> Tuple[str, bool]:
    """无反馈通道路径的末端兜底：命中则**丢弃**正文，不代答。

    框架不替人格说话（§1.9）：有 run 的路径一律让人格自己重说一句（见
    ``GsCoreAIAgent._ooc_recover_persona_voice``）；这里只给没有 run 的出口
    （proactive 播报、``send_chat_result`` 末端）用——返回空串即「不发」。
    返回 ``(输出文本, 是否被丢弃)``。
    """
    hit = check_ooc(text, tier, user_text=user_text)
    if hit is None:
        return text, False
    return "", True


def is_tech_dump(text: str) -> bool:
    """工具/子代理返回是否为堆栈或状态码技术 dump（供 tool return 入模前屏蔽）。"""
    if not text or not text.strip():
        return False
    if _TECH_DUMP_RE.search(_CODE_FENCE_RE.sub(" ", text)):
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
