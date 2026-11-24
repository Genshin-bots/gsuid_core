"""词面召回问句分析与分词原语（无库/Episode 依赖）。"""

from __future__ import annotations

import re

from gsuid_core.ai_core.control.lines import CLOCK_LINE_RE

_OVERLAP_TOKEN_RE = re.compile(
    r"[A-Za-z]{3,}(?:-[A-Za-z]{3,})+|"
    r"[A-Z]{3,}|"
    r"[A-Za-z]{4,}|"
    r"[A-Za-z]+\d+[A-Za-z0-9+\-#]*|"
    r"\d+[A-Za-z]+[A-Za-z0-9+\-#]*|"
    r"\$?\d{1,3}(?:,\d{3})+|"
    r"\d+(?:st|nd|rd|th)|"
    r"\d{2,4}|"
    r"[一-鿿]{2,}"
)


_QUERY_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "have",
        "been",
        "some",
        "about",
        "your",
        "from",
        "they",
        "them",
        "what",
        "when",
        "would",
        "could",
        "should",
        "please",
        "there",
        "their",
        "where",
        "which",
        "while",
        "after",
        "before",
        "into",
        "just",
        "more",
        "than",
        "very",
        "really",
        "like",
        "the",
        "and",
        "for",
        "can",
        "you",
        "any",
        "not",
        "but",
        "how",
        "who",
        "why",
        "are",
        "was",
        "did",
        "has",
        "had",
        "our",
        "out",
        "all",
        "new",
        "now",
        "get",
        "got",
        "use",
        "also",
        "still",
        "even",
        "only",
        "much",
        "many",
        "other",
        "another",
        "something",
        "anything",
        "help",
        "give",
        "make",
        "want",
        "need",
        "find",
        "best",
        "good",
        "great",
        "maybe",
        "think",
        "going",
        "told",
        "tell",
        "chat",
        "back",
        "suggest",
        "recommend",
        "upcoming",
        "conversation",
    }
)


_HIT_GENERIC = frozenset(
    {
        "class",
        "classes",
        "session",
        "sessions",
        "times",
        "time",
        "week",
        "weeks",
        "day",
        "days",
        "month",
        "months",
        "year",
        "years",
        "item",
        "items",
        "thing",
        "things",
        "list",
        "lists",
        "type",
        "types",
        "kind",
        "kinds",
        "event",
        "events",
        "user",
        "assistant",
        "today",
        "tomorrow",
        "yesterday",
        "schedule",
        "scheduled",
        "plan",
        "plans",
        "activity",
    }
)


# 时钟前缀/墙上时钟行，不能进词面 token。
_CLOCK_PREFIX_RE = re.compile(r"^(?:当前时间[：:]\s*[^\n]+\n+)+")


_SHORT_TOKEN_RES: dict[str, re.Pattern[str]] = {}


_SUM_RE = re.compile(
    r"\bhow much total\b|\btotal money\b|\bspent on\b|\bexpenses\b|一共花|总共花|合计|"
    r"\bhow many (?:hours?|days?)\b.{0,80}\b(?:in total|altogether)\b|"
    r"\btotal (?:number|sum) of\b|summed across",
    re.IGNORECASE,
)


# 问句自己点名多份材料。裸 list/which/for each、合计、是多少不算。
_DOC_NOUN = r"(?:reports?|files?|documents?|tables?|materials?|材料|文件|文档|报告|表格)"


_SET_QUERY_RE = re.compile(
    r"\b(?:list (?:all|every) (?:the )?" + _DOC_NOUN + r"|"
    r"across (?:all |these |those |the )?" + _DOC_NOUN + r"|"
    r"among (?:all |these |those |the )?" + _DOC_NOUN + r"|"
    r"each of (?:these |those )?" + _DOC_NOUN + r"|"
    r"every one of (?:these |those )?" + _DOC_NOUN + r"|"
    r"for each " + _DOC_NOUN + r"|"
    r"which of (?:these|those) " + _DOC_NOUN + r")\b|"
    r"这些" + _DOC_NOUN + r"|"
    r"几份" + _DOC_NOUN + r"|"
    r"各(?:份|篇)|每(?:份|篇)|哪(?:一|几)份|"
    r"哪些" + _DOC_NOUN + r"|"
    r"所有" + _DOC_NOUN + r"|"
    r"跨(?:文件|材料|文档|报告)|多份",
    re.IGNORECASE,
)


def strip_clock_lines(query: str) -> str:
    """剥时钟前缀和墙上时钟行，留给问句本身。"""
    body = _CLOCK_PREFIX_RE.sub("", (query or "").strip())
    body = CLOCK_LINE_RE.sub("", body)
    return body.strip()


def sql_like_tokens(tokens: list[str]) -> list[str]:
    """LIKE 不用短英文：%led% 会命中 settled，灌满最近一条会话。"""
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        key = tok.lower()
        if key in seen or key in _HIT_GENERIC:
            continue
        has_digit = any(c.isdigit() for c in tok)
        if tok.isascii() and " " not in tok and "-" not in tok and len(tok) < 4 and not has_digit:
            continue
        seen.add(key)
        out.append(tok)
    return out


def token_in_text(tok: str, blob: str) -> bool:
    """短英文按 ASCII 词边界匹配，避免 game 命中 gaming。

    不用 ``\\b``：Python 把汉字算进 ``\\w``，英文贴着中文时边界会消失。
    """
    key = tok.lower()
    if not tok.isascii() or " " in tok or "-" in tok or len(tok) >= 8 or any(c.isdigit() for c in tok):
        return key in blob
    compiled = _SHORT_TOKEN_RES[key] if key in _SHORT_TOKEN_RES else None
    if compiled is None:
        compiled = re.compile(rf"(?<![a-z0-9_]){re.escape(key)}(?![a-z0-9_])")
        _SHORT_TOKEN_RES[key] = compiled
    return compiled.search(blob) is not None


def query_overlaps_text(query: str, text: str) -> bool:
    """query 实词是否出现在 text。无实词则不过滤（避免空问句把偏好全掐掉）。"""
    toks = query_tokens(query)
    if not toks:
        return True
    blob = (text or "").lower()
    return any(token_in_text(tok, blob) for tok in toks)


_NEEDLE_RE = re.compile(
    r"[A-Z][a-z]{2,}|"
    r"[A-Z]{2,}|"
    r"\d+(?:\.\d+)+|"
    r"\$\d+(?:,\d{3})*(?:\.\d+)?|"
    r"\d{2,}"
)


_LEAD_SKIP = frozenset(
    {
        "the",
        "what",
        "how",
        "does",
        "have",
        "can",
        "could",
        "would",
        "will",
        "is",
        "are",
        "do",
        "did",
        "please",
        "why",
        "when",
        "where",
        "who",
        "which",
        "i",
    }
)


def query_required_needles(query: str) -> list[str]:
    """专名/版本号/金额：有则命中正文必须带上，否则视为弱相关。"""
    q = strip_clock_lines(query or "")
    if not q:
        return []
    first, sep, rest = q.strip().partition(" ")
    # 只跳过英文疑问/助动词句首，不跳过 Johnny 这类专名。
    scan = rest if sep and first.lower() in _LEAD_SKIP else q
    seen: set[str] = set()
    out: list[str] = []
    for m in _NEEDLE_RE.finditer(scan):
        tok = m.group(0)
        key = tok.lower()
        if key in seen or key in _QUERY_STOPWORDS:
            continue
        seen.add(key)
        out.append(tok)
    return out


def text_has_query_needles(query: str, text: str) -> bool:
    """有专名/数字则必须命中；否则至少两个 4+ 字母实词对上，避免近邻充数。"""
    needles = query_required_needles(query)
    blob = (text or "").lower()
    if needles:
        return any(token_in_text(n, blob) for n in needles)
    toks = [t for t in query_tokens(query) if len(t) >= 4]
    if len(toks) < 2:
        return True
    hits = sum(1 for t in toks if token_in_text(t, blob))
    return hits >= 2


_HYPERNYM_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "sibling": ("brother", "sister"),
    "siblings": ("brother", "sister"),
    "parent": ("father", "mother"),
    "parents": ("father", "mother"),
    "grandparent": ("grandfather", "grandmother"),
    "grandparents": ("grandfather", "grandmother"),
    "child": ("son", "daughter"),
    "children": ("son", "daughter"),
    "furniture": ("table", "chair", "desk", "sofa", "couch", "bed", "mattress", "bookshelf", "cabinet"),
    "grocery": ("supermarket", "market", "store"),
    "groceries": ("supermarket", "market", "store"),
    "pet": ("dog", "cat", "puppy", "kitten"),
    "pets": ("dog", "cat", "puppy", "kitten"),
    "vehicle": ("car", "truck", "bike", "bicycle"),
    "vehicles": ("car", "truck", "bike", "bicycle"),
    "兄弟姐妹": ("哥哥", "弟弟", "姐姐", "妹妹"),
    "父母": ("爸爸", "妈妈", "父亲", "母亲"),
    "家具": ("桌", "椅", "床", "沙发", "柜"),
    "宠物": ("猫", "狗"),
}


def query_tokens(query: str) -> list[str]:
    """丢掉停用词和问句套话，短语只从实词相邻对来。"""
    query = strip_clock_lines(query)
    out: list[str] = []
    seen: set[str] = set()
    content: list[str] = []
    for m in _OVERLAP_TOKEN_RE.finditer(query):
        tok = m.group(0).replace("%", "").replace("\\", "")
        pieces = [tok]
        if "-" in tok and tok.isascii():
            pieces.extend([p for p in tok.split("-") if len(p) >= 2])
        if not tok.isascii() and len(tok) > 2 and "-" not in tok and not any(c.isdigit() for c in tok):
            pieces = [tok[i : i + 2] for i in range(len(tok) - 1)]
        for piece in pieces:
            key = piece.lower()
            has_digit = any(c.isdigit() for c in piece)
            if has_digit:
                min_len = 2
            elif piece.isascii():
                min_len = 3
            else:
                min_len = 2
            if len(piece) < min_len or key in _QUERY_STOPWORDS or key in seen:
                continue
            seen.add(key)
            out.append(piece)
            content.append(piece)
            if key in _HYPERNYM_EXPANSIONS:
                for syn in _HYPERNYM_EXPANSIONS[key]:
                    if syn.lower() not in seen:
                        seen.add(syn.lower())
                        out.append(syn)
            if len(out) >= 14:
                break
        if len(out) >= 14:
            break
    for i in range(len(content) - 1):
        phrase = f"{content[i]} {content[i + 1]}"
        key = phrase.lower()
        if len(phrase) < 8 or key in seen:
            continue
        seen.add(key)
        out.append(phrase)
        if len(out) >= 18:
            break
    return out


_COUNT_QUERY_RE = re.compile(
    r"\bhow many\b|"
    r"(?:一共|总共|合计|累计|总计|共).{0,12}(?:多少|几)|"
    r"多少(?:个|次|条|题|项|遍|件)|"
    r"(?:有|过|去过|买过|做过|聊过|提过|问过|发过|参加过|记录了?)几(?:个|次|条|题|项|遍|件)|"
    r"几次\s*[？?]",
    re.IGNORECASE,
)


_COUNT_EXCLUDE_RE = re.compile(
    r"多少钱|多少块|多少岁|多少度|多少号|"
    r"how much (?:is|does|for)|"
    r"how many (?:days?|weeks?|months?|hours?|minutes?|seconds?)\b|"
    r"多少[天周月小时分钟秒]",
    re.IGNORECASE,
)


def looks_like_count_query(query: str) -> bool:
    """过往数量/次数。排除多少钱/多少度等实时问价。"""
    q = strip_clock_lines(query or "")
    if not q or _COUNT_EXCLUDE_RE.search(q):
        return False
    return bool(_COUNT_QUERY_RE.search(q))


# 问句套话和度量词。原话说 78% / 250ms，不会写 percentage / average。
_ASK_FILLER = frozenset(
    {
        "summary",
        "comprehensive",
        "complete",
        "including",
        "everything",
        "throughout",
        "conversation",
        "conversations",
        "please",
        "could",
        "would",
        "across",
        "about",
        "percentage",
        "percent",
        "average",
        "between",
        "during",
        "specific",
        "current",
        "latest",
        "total",
        "number",
        "using",
        "based",
        "through",
        "within",
        "before",
        "after",
        "around",
        "different",
        "various",
        "already",
        "really",
        "trying",
    }
)


def attribute_content_tokens(query: str, *, limit: int = 8) -> list[str]:
    """问句里较长的实词，用来钉「同一件事」而不是整库最新一个数字。"""
    toks = [t for t in query_tokens(query) if " " not in t and len(t) >= 4 and t.lower() not in _ASK_FILLER]
    toks.sort(key=lambda t: (-len(t), t.lower()))
    out: list[str] = []
    seen: set[str] = set()
    for tok in toks:
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


# 合计/计数问句里的度量套话，不能拿来当主题词。
_TOPIC_SKIP = frozenset(
    {
        "total",
        "money",
        "spent",
        "spend",
        "expenses",
        "expense",
        "related",
        "since",
        "start",
        "year",
        "much",
        "many",
        "amount",
        "cost",
        "costs",
        "have",
        "been",
        "this",
        "that",
        "with",
        "from",
        "your",
        "about",
        "last",
        "month",
        "week",
        "currently",
        "including",
        "attended",
        "during",
        "before",
        "after",
        "something",
        "recently",
        "first",
        "last",
        "most",
        "long",
        "months",
        "weeks",
        "days",
        "years",
        "hours",
        "four",
        "three",
        "two",
        "one",
        "five",
        "second",
    }
)


def topic_pin_tokens(query: str, limit: int = 2) -> list[str]:
    """主题词。连字符拆开后再丢掉合计套话，没有再退回问句实词。"""
    raw = attribute_content_tokens(query, limit=12)
    pieces: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        parts = tok.split("-") if "-" in tok else [tok]
        for part in parts:
            key = part.lower()
            if len(part) < 3 or key in seen:
                continue
            seen.add(key)
            pieces.append(part)
    kept = [t for t in pieces if t.lower() not in _TOPIC_SKIP]
    if not kept:
        kept = pieces or raw
    return kept[:limit]


def looks_like_set_query(query: str, *, memory_intent: str = "point_latest") -> bool:
    """跨多份材料的清单/比较/聚合。时间线排序/全历程摘要不算。"""
    body = strip_clock_lines(query or "")
    if not body or memory_intent in ("timeline_order", "broad_summary"):
        return False
    return bool(_SET_QUERY_RE.search(body))


def looks_like_sum_query(query: str, *, memory_intent: str = "point_latest") -> bool:
    """多笔加总。时间线排序与计数不算。"""
    body = strip_clock_lines(query or "")
    if not body or memory_intent == "timeline_order" or looks_like_count_query(body):
        return False
    return bool(_SUM_RE.search(body))


# 问句虚词/套话不能当栏目 focus。单字（的了吗呢）已由长度门丢掉。
_DOC_TOKEN_SKIP = frozenset(
    {
        "这个",
        "那个",
        "这些",
        "那些",
        "哪些",
        "哪个",
        "哪种",
        "这样",
        "那样",
        "这么",
        "那么",
        "这里",
        "那里",
        "这儿",
        "那儿",
        "我们",
        "你们",
        "他们",
        "她们",
        "它们",
        "咱们",
        "大家",
        "自己",
        "别人",
        "其他",
        "其它",
        "有人",
        "什么",
        "怎么",
        "如何",
        "为何",
        "多少",
        "几个",
        "为什么",
        "怎么样",
        "是不是",
        "有没有",
        "能不能",
        "会不会",
        "要不要",
        "对不对",
        "好不好",
        "行不行",
        "已经",
        "正在",
        "将要",
        "即将",
        "一直",
        "仍然",
        "还是",
        "就是",
        "不是",
        "没有",
        "不会",
        "不能",
        "不要",
        "不用",
        "是否",
        "非常",
        "十分",
        "比较",
        "更加",
        "几乎",
        "大概",
        "可能",
        "应该",
        "必须",
        "一定",
        "当然",
        "其实",
        "确实",
        "似乎",
        "好像",
        "现在",
        "目前",
        "当前",
        "当时",
        "以前",
        "之后",
        "然后",
        "接着",
        "于是",
        "都是",
        "也是",
        "只是",
        "以及",
        "并且",
        "而且",
        "或者",
        "但是",
        "可是",
        "然而",
        "因此",
        "所以",
        "因为",
        "如果",
        "假如",
        "虽然",
        "尽管",
        "无论",
        "不管",
        "另外",
        "此外",
        "同时",
        "否则",
        "对于",
        "关于",
        "根据",
        "按照",
        "作为",
        "由于",
        "通过",
        "经过",
        "针对",
        "基于",
        "除了",
        "包括",
        "其中",
        "之中",
        "之间",
        "之内",
        "之外",
        "以上",
        "以下",
        "以内",
        "以外",
        "之前",
        "可以",
        "能够",
        "需要",
        "应当",
        "得以",
        "进行",
        "加以",
        "予以",
        "给予",
        "一个",
        "一些",
        "一种",
        "一类",
        "有些",
        "所有",
        "每个",
        "各个",
        "各种",
        "全部",
        "部分",
        "任何",
        "若干",
        "相关",
        "有关",
        "所谓",
        "如下",
        "上述",
        "等等",
        "之类",
        "请问",
        "分别",
        "总共",
        "一共",
        "having",
        "also",
        "being",
        "those",
        "these",
        "each",
        "such",
        "both",
        "same",
        "other",
        "others",
    }
)


def wants_parallel_document_coverage(query: str, *, memory_intent: str = "point_latest") -> bool:
    """比较或汇总多份材料。时间线与加总不走这条。"""
    body = strip_clock_lines(query or "")
    if not body or memory_intent in ("timeline_order", "broad_summary"):
        return False
    if looks_like_sum_query(body, memory_intent=memory_intent):
        return False
    return looks_like_set_query(body, memory_intent=memory_intent)


_HAS_DIGIT_RE = re.compile(r"\d")


_ORDINAL_RE = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b|第\s*(\d{1,4})", re.IGNORECASE)


def ordinal_index(query: str) -> str:
    """问句里的序数。英文 27th，中文第 27 项。"""
    found = _ORDINAL_RE.search(query or "")
    if found is None:
        return ""
    return found.group(1) or found.group(2) or ""


_SPREAD_FRAME = frozenset(
    {
        "times",
        "weeks",
        "days",
        "months",
        "minutes",
        "playing",
        "past",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "several",
        "earliest",
        "latest",
        "order",
        "visited",
        "classes",
        "class",
        "take",
        "taking",
        "going",
        "looking",
        "want",
        "wanted",
        "need",
        "needed",
        "help",
        "please",
        "some",
        "more",
        "good",
        "best",
        "just",
        "really",
        "think",
        "thinking",
        "trying",
        "actually",
        "recommend",
        "suggestion",
        "suggestions",
        "tips",
    }
)


def spread_topic_tokens(query: str) -> list[str]:
    """铺开用的主题词。去掉次数、时间单位，避免 times/weeks 把 bake 挤掉。"""
    raw = topic_pin_tokens(query, limit=6)
    kept = [t for t in raw if t.lower() not in _SPREAD_FRAME]
    out = (kept or raw)[:2]
    ordinal = ordinal_index(query)
    if ordinal and ordinal.lower() not in {t.lower() for t in out}:
        out.append(ordinal)
    return out


def fact_sweep_tokens(query: str) -> list[str]:
    """事实清单检索词：比 spread 多留 1–2 个实词。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import temporal_search_query

    body = strip_clock_lines(query or "")
    topic = temporal_search_query(body) or body
    merged: list[str] = []
    seen: set[str] = set()
    for tok in spread_topic_tokens(body) + topic_pin_tokens(topic, limit=6):
        key = tok.lower()
        if key in _SPREAD_FRAME or key in seen:
            continue
        seen.add(key)
        merged.append(tok)
        if len(merged) >= 4:
            return merged
    for tok in attribute_content_tokens(body, limit=6):
        key = tok.lower()
        if key in _SPREAD_FRAME or key in seen or len(tok) < 4:
            continue
        seen.add(key)
        merged.append(tok)
        if len(merged) >= 5:
            break
    return merged[:5]


def token_search_forms(tok: str) -> list[str]:
    """复数问句也搜单数，Film Festival 对得上 festivals。"""
    forms = [tok]
    low = tok.lower()
    if low.endswith("s") and len(low) > 4 and not low.endswith("ss"):
        forms.append(tok[:-1])
    return forms
