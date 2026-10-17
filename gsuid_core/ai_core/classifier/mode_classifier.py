"""单句意图：闲聊 / 工具 / 问答。

不加载 sklearn 与 jieba。词表只提供通用语言学范畴；插件域词不进这里。
"""

import re
from typing import Literal, Protocol, Sequence, TypedDict

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.control.lines import MSG_SECTION_MARK

# 省略式跟进：单句无独立语义，必须靠上文才知道动作（语言学闭类 + 极短回指）
_ELLIPSIS_FOLLOWUP_RE = re.compile(
    r"^(然后呢|那呢|还有呢|接着呢|后来呢|结果呢|怎么样了|怎么了|呢{1,2}|继续|还有吗|"
    r"取消|不要了|那个呢|这个呢)[\s\W]*$"
    r"|^(改成|改为|改到|换成).{0,16}$",
    re.I,
)

_NEGATED_ACTION_RE = re.compile(r"(不要|别|不用|禁止).{0,3}(查|看|画|生成|制作|做图|搜|找|提醒|打开|调用|搜索)")
_CANCEL_RE = re.compile(r"^(取消|不要了|算了|不用了|别了)[\s\W]*$")
_POLITE_PREFIX_RE = re.compile(r"^(请|麻烦|能不能|可不可以|可以)+")
_IDENTITY_RE = re.compile(r"(请问|问下|问一下)?你(是|在|用|对|觉得|认为|喜欢|自己|多少|几岁|多大)")
_VAGUE_POINTER_RE = re.compile(r"^(这|那|它)(是|个)?(什么鬼|什么|啥|鬼)[?？!！]*$")
_HOW_ABOUT_RE = re.compile(r"怎么回事")
# 动词须在句首或「帮我」后；「漫画」「画图真不错」不算指令。
_MEDIA_CMD_RE = re.compile(
    r"(?:^|[，,。！？\s]|帮我|给我|请|麻烦)"
    r"(?:生成|制作|合成|来首|来个|来张|来一|来点|画一张|画个|画一|画一下)"
    r".{0,12}"
    r"(?:图|图片|照片|图标|视频|语音|歌|歌曲|音乐|音频|壁纸|头像|表情)"
)
_MAKE_RE = re.compile(r"(画|生成|制作|合成)(?!了).{0,3}(一张|一个|一首|一段|一套)")
_EDIT_CMD_RE = re.compile(
    r"(换背景|换个背景|(?:^|帮我|请).{0,2}把.{0,12}换成(?!了)|去水印|去掉水印|去掉.{0,8}水印|水印去掉|"
    r"加水印|加个水印|加个字|把字去掉|字去掉|P一下|P图(?!了)|加两个字|写上|修改这张)"
)
_WANT_MEDIA_RE = re.compile(r"(我要|想要).{0,4}(一|两|\d+)(张|个|首|段).{0,16}(图|图片|照片|视频|语音|歌|音乐)")
_REMIND_RE = re.compile(r"(提醒我|通知我|叫我).{0,12}(点|时|开会|起来|起床|一下)")
# 分享卡和「，直接调用」这类叙述不是指令。整句得从祈使开始。
_INVOKE_RE = re.compile(r"^(?:你)?(?:直接)?(?:调用|打开|启动|运行)(?:一下|下)?(?!起来|不了|不上).{0,28}$")
_INVOKE_ASIDE_RE = re.compile(r"你就|不香|开撸")
_HELP_RE = re.compile(
    r"(?:帮我|给我|帮忙).{0,8}"
    r"(?:看一下|看下|看看|查一下|查下|算一下|画一下|画一张|画个|做一张|搜一下|打开|写一下|写一个)"
)
_SEARCH_RE = re.compile(r"搜一下|搜索一下")
_ASSIST_RE = re.compile(
    r"(?:帮我|给我|请).{0,4}(?:记一下|记下来|翻译一下|总结一下|概括一下|对比一下|比较一下)"
    r"|^(?:\S{0,3})?(?:翻译一下|总结一下|概括一下|对比一下|比较一下|记一下|记下来)"
)
_DELIVER_RE = re.compile(r"(?:给我|帮我).{0,6}(?:报告|原文|截图|文件)")
_BARE_CHECK_RE = re.compile(r"(?<![排检])查一下(?!就)|(?<![排检])查下")
_LOOKUP_RE = re.compile(r"(?:今天|现在|当前|明天|当地|这里)?(?:的)?(?:天气|气温)(?:怎么样|如何)")
_QA_RE = re.compile(
    r"(怎么用|怎么打|怎么触发|怎么完成|怎么获得|怎么解锁|怎么弄|怎么连接|怎么安装|"
    r"怎么设置|怎么配置|怎么登录|怎么登陆|怎么部署|怎么下载|怎么导入|怎么导出|"
    r"怎么绑定|怎么接入|怎么对接|怎么编译|如何|在哪|从哪|哪儿|哪里|是啥|是什么|"
    r"多少|出自|有什么用|干嘛的|带什么|带啥|配什么|配啥|该不该|(?<!谁)是谁)"
)
# 这些是陈述或口头禅，不是在向人要一个答案。
_NARRATIVE_QA_RE = re.compile(
    r"多少是多少|没多少|没有多少|不了多少|能省多少|能贷多少|能用多少|不知道多少(?!钱)|"
    r"看可以充多少|有多少底|什么机制都能|不是什么|什么人都|什么鸟人|用着如何|生意如何|"
    r"收获如何|串的如何|花生如何|砂锅如何|看人介绍|从哪听来|从哪整来|我哪里|大白天哪里|"
    r"好不到哪里|不能想去哪|烧不了多少|谁还去买|是啥就是啥|哪里都|多少有些|什么什么|"
    r"贵多少|沾点金融|连续多少次|成交量也没|亏也没亏|放大了不知道多少|轻不了多少|"
    r"去哪里都|忘了是什么|哪里赚钱哪里|熟到哪里|多少人说|多少年前|"
    r"游艇在哪里|还是啥的|怎么用囖|有多少购买力|多少搞了|哪里去了|没东西吃在哪|"
    r"退多少年|玩的是什么|不是什么经济|买多少的车|交付数量没多少|是什么车|"
    r"没看到过多少|具体怎么弄|还得看送多少|有的多少|玩起来什么感觉|不知道扣多少|能有多少|"
    r"是什么心理|是什么时区|不知道具体有多少|想怎么用|不知道用量|降低多少|不知道是多少|"
    r"可以看做是|哪里经得住|不知道要花|看是谁|都不能断|还是谁之前|能增长多少|没亏多少|"
    r"哪里差异|备份多少次|不好说多少|断哪儿|想下载多少|哪里拼得过|没重多少|给多少撤|"
    r"哪里亮了|得多少年|哪里好了|不记得当时多少|多少还有点|我是谁我在那|没哪里收入|"
    r"哪里哪里|到手多少|不是技术是什么|明写在哪个|碳纳米管|是谁邀请|130线|还是多少$"
)
_DRAW_BARE_RE = re.compile(r"^画个\S{1,16}$")
_YOU_WHERE_RE = re.compile(r"你在哪")
_QUESTION_RE = re.compile(r"(吗|么|呢)[?？!！]*$|[?？]")
_CHECK_VERBS = (
    "看一下",
    "搜一下",
    "搜索一下",
    "分析一下",
    "计算一下",
    "显示一下",
    "获取一下",
    "看下",
)


def _is_ellipsis_followup(text: str) -> bool:
    """当前句是否为「必须接上文才有动作」的省略跟进。"""
    body = (text or "").strip()
    if not body or len(body) > 24:
        return False
    if _ELLIPSIS_FOLLOWUP_RE.match(body):
        return True
    # 极短回指：「呢？」「咋样」「怎样了」；不含「是吗/可以吗」（纯确认，非未完成动作）
    return bool(re.fullmatch(r".{0,4}(呢|咋样|怎样)[？?！!\s]*", body))


class _UserTurnRecord(Protocol):
    # 与 MessageRecord.role 的 Literal 对齐，避免 Protocol 可变属性不变性冲突
    role: Literal["user", "assistant", "system"]
    user_id: str
    content: str


def collect_prior_user_turns(
    records: Sequence[_UserTurnRecord],
    current_user_id: str,
    *,
    max_turns: int = 4,
) -> list[str]:
    """从 MessageRecord 列表提取同一用户的近期原文（旧→新，不含本轮未入库句）。"""
    out: list[str] = []
    for rec in records:
        role = rec.role
        uid = str(rec.user_id or "")
        content = rec.content
        if role != "user" or not isinstance(content, str) or not content.strip():
            continue
        if uid and current_user_id and uid != str(current_user_id):
            continue
        # 去掉过长 payload 头，只取消息体
        body = content
        if MSG_SECTION_MARK in body:
            body = body.split(MSG_SECTION_MARK, 1)[-1]
        # 兼容新旧时间行：【当前时间】xxx / [当前时间：xxx]
        for time_sep in ("[当前时间：", "[当前时间:", "【当前时间】"):
            if time_sep in body:
                body = body.split(time_sep, 1)[0]
                break
        body = body.strip()
        if body:
            out.append(body)
    return out[-max_turns:]


# 纪律：不收录任何业务插件专属域词（由插件工具描述召回，不进框架意图词表）。
FUNCTIONAL_NOUNS = {
    "面板",
    "数据",
    "属性",
    "排行",
    "排行榜",
    "榜单",
    "记录",
    "战绩",
    "价格",
    "气温",
    "天气",
    "配置",
    "装备",
    "评分",
    "详情",
    "状态",
    "数值",
    "倍率",
    "概率",
    "掉落",
    "成本",
    "收益",
    "库存",
    "余额",
    "伤害",
}

KNOWLEDGE_NOUNS = {
    "机制",
    "剧情",
    "队伍",
    "武器",
    "背景",
    "故事",
    "介绍",
    "弱点",
    "位置",
    "材料",
    "配方",
    "出处",
    "世界观",
    "天赋",
    "技能",
    "成就",
    "任务",
    "彩蛋",
    "设定",
    "攻略",
    "评价",
    "强度",
    "身高",
    "生日",
    "声优",
    "血量",
}

STATE_WORDS = {
    "麻",
    "麻了",
    "亏",
    "亏麻",
    "亏死",
    "救命",
    "卧槽",
    "牛逼",
    "笑死",
    "无语",
    "666",
    "丑",
    "太丑",
    "真丑",
    "难看",
    "好丑",
    "垃圾",
    "坑",
    "药丸",
    "崩",
    "崩了",
    "难",
    "好难",
    "强",
    "弱",
    "离谱",
    "恶心",
    "卡",
    "慢",
    "贵",
    "便宜",
    "好",
    "坏",
    "高",
    "低",
    "烂",
    "好烂",
    "拉胯",
    "寄",
    "晦气",
    "谢谢",
    "thanks",
    "ok",
    "懂",
    "明白",
    "晕",
    "懵",
    "喜欢",
    "爱",
    "讨厌",
    "烦",
    "一般",
    "行",
    "不行",
    "鬼",
}


class IntentHit(TypedDict):
    text: str
    intent: str
    conf: float
    reason: str


def _hit(text: str, intent: str, conf: float, reason: str) -> IntentHit:
    return {"text": text, "intent": intent, "conf": conf, "reason": reason}


def _has_word(text: str, words: set[str]) -> bool:
    return any(word in text for word in words)


def _check_near_functional(text: str) -> bool:
    for noun in FUNCTIONAL_NOUNS:
        start = 0
        while True:
            idx = text.find(noun, start)
            if idx < 0:
                break
            window = text[max(0, idx - 8) : idx + len(noun) + 6]
            if any(verb in window for verb in _CHECK_VERBS):
                return True
            start = idx + len(noun)
    return False


def _search_command(text: str) -> bool:
    if _SEARCH_RE.search(text) is None:
        return False
    # 「搜一下……全是」是在讲搜完的结果。
    return re.search(r"搜一下.{0,16}(全是|都是|还是)", text) is None


def _invoke_command(text: str) -> bool:
    if len(text) > 40 or _INVOKE_ASIDE_RE.search(text):
        return False
    if re.search(r"(打开|启动|运行|调用).{0,16}会", text):
        return False
    return _INVOKE_RE.search(text) is not None


def _assist_command(text: str) -> bool:
    # 长句里顺口提到「翻译一下」多半在讲别的事。
    if len(text) > 32:
        return False
    return _ASSIST_RE.search(text) is not None


def _tool_reason(text: str) -> str:
    text = _POLITE_PREFIX_RE.sub("", text).strip() or text
    if _MAKE_RE.search(text) or _DRAW_BARE_RE.search(text):
        return "media command"
    if _MEDIA_CMD_RE.search(text) or _WANT_MEDIA_RE.search(text):
        return "media command"
    if _EDIT_CMD_RE.search(text):
        return "edit command"
    if _REMIND_RE.search(text):
        return "remind command"
    if _assist_command(text):
        return "assist command"
    if _DELIVER_RE.search(text):
        return "deliver command"
    if _invoke_command(text):
        return "invoke command"
    if _SEARCH_RE.search(text) and _search_command(text):
        return "search command"
    if _HELP_RE.search(text):
        return "help command"
    if _check_near_functional(text) or (len(text) <= 48 and _BARE_CHECK_RE.search(text)):
        return "check data"
    return ""


def _drop_negated_action(text: str) -> str:
    """否定只吃掉紧挨着的动作。后面没有新指令时返回空串。"""
    body = text
    for _ in range(3):
        match = _NEGATED_ACTION_RE.search(body)
        if match is None:
            return body
        rest = (body[: match.start()] + body[match.end() :]).strip(" ，,。；;、")
        if rest == "" or (_tool_reason(rest) == "" and not _knowledge_question(rest)):
            return ""
        body = rest
    return body


def _is_question(text: str) -> bool:
    return _QUESTION_RE.search(text) is not None or "怎么样" in text or "如何" in text


def _ma_near_noun(text: str) -> bool:
    # 「怎么样 / 什么」里的么不是句末疑问。
    match = re.search(r"[吗么][?？!！]*$", text)
    if match is None:
        return False
    window = text[max(0, match.start() - 6) : match.start()]
    return _has_word(window, FUNCTIONAL_NOUNS) or _has_word(window, KNOWLEDGE_NOUNS)


def _knowledge_question(text: str) -> bool:
    if _YOU_WHERE_RE.search(text):
        return False
    # 带链接或标题壳的长贴是在转发，不是在发问。
    if len(text) > 40 and ("http" in text or "b23.tv" in text or text.startswith("【")):
        return False
    # 长贴后半段才出现的「多少 / 哪里」是叙述，不是在发问。
    scoped = text[:36] if len(text) > 48 else text
    # 句末真在问时保留；「用着如何」「还是多少」仍当叙述。
    narrative = _NARRATIVE_QA_RE.search(text) is not None
    ends_q = re.search(r"[？?吗]$", text) is not None
    forced = re.search(r"用着如何|还是多少$", text) is not None
    if narrative and (forced or not ends_q):
        return False
    if _QA_RE.search(scoped):
        return True
    if _ma_near_noun(scoped):
        return True
    if not _has_word(scoped, KNOWLEDGE_NOUNS):
        return False
    if "怎么样" in scoped or _intro_asks(scoped) or re.search(r"(?<!为)什么", scoped):
        return True
    return False


def _intro_asks(text: str) -> bool:
    if "介绍一下" not in text and "介绍个" not in text:
        return False
    others = KNOWLEDGE_NOUNS - {"介绍"}
    return _has_word(text, others)


def classify_sentence(text: str) -> IntentHit:
    """把单句分成闲聊、工具或问答。不读模型文件。"""
    raw = text or ""
    body = raw.strip()
    if not body:
        return _hit(raw, "闲聊", 0.5, "empty")
    if _CANCEL_RE.match(body):
        return _hit(raw, "闲聊", 0.96, "negated action")
    body = _drop_negated_action(body)
    if not body:
        return _hit(raw, "闲聊", 0.96, "negated action")
    ident = _IDENTITY_RE.search(body)
    if ident is not None and not _knowledge_question(body[: ident.start()]):
        return _hit(raw, "闲聊", 0.98, "greeting or identity")
    if _HOW_ABOUT_RE.search(body):
        return _hit(raw, "闲聊", 0.98, "greeting or identity")
    if _VAGUE_POINTER_RE.search(body):
        return _hit(raw, "闲聊", 0.95, "vague pointer")
    tool = _tool_reason(body)
    if tool:
        return _hit(raw, "工具", 0.98, tool)
    if _LOOKUP_RE.search(body):
        return _hit(raw, "工具", 0.9, "lookup question")
    if _has_word(body, FUNCTIONAL_NOUNS) and _has_word(body, STATE_WORDS) and not _is_question(body):
        return _hit(raw, "闲聊", 0.95, "status plus emotion")
    if _knowledge_question(body):
        return _hit(raw, "问答", 0.96, "knowledge question")
    return _hit(raw, "闲聊", 0.86, "default chat")


class IntentService:
    def _sync_predict(self, text: str) -> IntentHit:
        return classify_sentence(text)

    async def predict_async(
        self,
        text: str,
        *,
        prior_user_turns: list[str] | None = None,
        prev_turn_used_tools: bool = False,
    ) -> IntentHit:
        """异步预测意图（带同用户上下文）。

        短句先拼同用户上文再判；省略跟进且上轮用过工具时升为工具。
        低置信闲聊且带疑问词时，才试一次知识库。
        """
        text_s = (text or "").strip()
        if _CANCEL_RE.match(text_s):
            return _hit(text, "闲聊", 0.97, "cancel")
        priors = [p.strip() for p in (prior_user_turns or []) if p and p.strip()]
        is_short = len(text_s) <= 24
        is_ellipsis = _is_ellipsis_followup(text_s)

        def keep_current(hit: IntentHit, prefix: str) -> IntentHit:
            return _hit(text, hit["intent"], hit["conf"], f"{prefix}:{hit['reason']}")

        if priors and (is_short or is_ellipsis):
            joined = "\n".join([*priors[-3:], text_s])
            result = keep_current(self._sync_predict(joined), "ContextPrimary")
            if result["intent"] == "闲聊":
                solo = self._sync_predict(text_s)
                if solo["intent"] in ("工具", "问答") and solo["conf"] >= 0.55:
                    result = _hit(text, solo["intent"], solo["conf"], f"SoloOverride:{solo['reason']}")
        else:
            result = self._sync_predict(text_s)

        if result["intent"] == "闲聊" and priors and not (is_short or is_ellipsis):
            joined = "\n".join([*priors[-2:], text_s])
            result2 = self._sync_predict(joined)
            if result2["intent"] in ("工具", "问答") and result2["conf"] >= 0.45:
                result = keep_current(result2, "ContextJoin")

        if result["intent"] == "闲聊":
            if _CANCEL_RE.match(text_s):
                result = _hit(text, "闲聊", 0.97, "cancel")
            elif is_ellipsis and prev_turn_used_tools:
                result = _hit(text, "工具", 0.92, "Structural: ellipsis follow-up after tools")
            elif prev_turn_used_tools and (is_short or is_ellipsis) and result["conf"] < 0.9:
                result = _hit(text, "工具", 0.88, "Structural: short/ambiguous after tools")
            elif is_ellipsis and priors:
                prior_toolish = False
                for prior in reversed(priors[-3:]):
                    pr = self._sync_predict(prior)
                    if pr["intent"] in ("工具", "问答") and pr["conf"] >= 0.5:
                        prior_toolish = True
                        break
                if prior_toolish:
                    result = _hit(text, "工具", 0.9, "Structural: ellipsis after prior toolish user turns")

        if result["intent"] == "闲聊" and result["conf"] < 0.8:
            query_marks = ("什么", "怎么", "多少", "哪", "谁", "吗", "呢", "？", "?")
            if any(mark in text for mark in query_marks):
                try:
                    from gsuid_core.ai_core.rag import query_knowledge
                    from gsuid_core.ai_core.rag.skills_kb import SKILLS_DOC_SOURCE

                    hits = await query_knowledge(
                        query=text,
                        limit=1,
                        exclude_sources=[SKILLS_DOC_SOURCE],
                    )
                except Exception as e:
                    logger.trace(t("log.ai.vector_fallback_retrieval", e=e))
                    return result
                if hits and len(hits) > 0:
                    payload = hits[0].payload
                    if not isinstance(payload, dict):
                        return result
                    title = payload["title"] if "title" in payload else "Unknown"
                    logger.info(t("log.ai.vector_fallback_hit_text", text=text, p0=title))
                    return _hit(text, "问答", round(float(hits[0].score), 4), "VectorHit")

        return result


classifier_service = IntentService()
