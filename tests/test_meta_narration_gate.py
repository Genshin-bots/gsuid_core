"""过程元叙述闸：把自身检索机制当记忆事实讲给用户 = 出戏（OOC 事故 2026-09）。

三条群聊实发原句（此处只保留**形态**，不带真实称呼与他人私事）：

1. ``…没查到。你教的原话没存下来。剩下的只有别人提过的那句。``
2. ``…内部库没你要的那个数。不是 0，是压根没记过。``
3. ``我只翻了你那页。别人的不归我翻。``

修法分三层，缺一层就复发：

- **不生产**（``facade.render_cognition_block`` / ``find_tools`` / ``SYSTEM_CONSTRAINTS``）——
  框架自己不再把检索术语和工具名递到语言侧，也不再规定「失败只说没查到」。
- **形态**（``chat_style.ANSWER_CONTRACT``）——认「不知道」时只给结论，
  不解释这份「不知道」的性质/来源/范围（这是 LLM 最纯的指纹）。
- **兜住**（``output_firewall.meta_narration``）——同一小句里
  「自指内部机制 × 机制名词 × 缺失谓词」三信号共现才判，纯结构，不吃人格词表。

**为什么第 1、3 条不靠闸拦**：要拦它们就得对「没查到 / 翻页 / 记着」下词表，
而人话里「翻不到卷轴…先睡了」和它们同形（见 ``test_benign_fp``）。AGENTS §1.9
禁止框架用人格/业务词表判风格，所以这两条只能靠「不生产 + 形态约束」根治；
本文件把这一取舍显式锁住，防止后人误以为闸能接住。
"""

from __future__ import annotations

from pathlib import Path

from gsuid_core.ai_core.output_firewall import (
    NEVER_RELEASE_CATEGORIES,
    FirewallHit,
    check_ooc,
    build_rewrite_warning,
    looks_like_meta_narration,
)

# 事故原句只保留**形态**（缺材料 + 解释缺失来源 + 缺失的对比式定义），不带真实
# 称呼与他人私事：断言要的是这个形状，原文进仓库等于把群友信息固化。
LEAK_1 = "…没查到。你教的原话没存下来。剩不下别的了，只有别人提过的那句。呼。"
LEAK_2 = "…内部库没你要的那个数。不是 0，是压根没记过。呼…睡去了。"
LEAK_3 = "我只翻了你那页。别人的不归我翻。呼。"

# 闸能结构化命中的同族形态（都带「自指内部机制 + 机制名词 + 缺失」）
STRUCTURAL_HITS = (
    LEAK_2,
    "我数据库里没这条",
    "系统日志没有这一轮",
    "我这边资料库没翻到你的分值",
    "内部索引里没有这个人",
)


def _meta_hit() -> FirewallHit:
    return FirewallHit(category="meta_narration", matched=["内部机制自述"])


def test_structural_leak_is_never_released() -> None:
    """meta_narration 走 never-release：提醒后仍不许原样出站。"""
    assert "meta_narration" in NEVER_RELEASE_CATEGORIES
    warning = build_rewrite_warning(_meta_hit())
    assert "此刻没有可用材料" in warning
    assert "本来就不存在" in warning


def test_meta_narration_catches_the_mechanism_leak() -> None:
    """生产第 2 条（含同族变体）必须被拦，且归到 meta_narration。"""
    for text in STRUCTURAL_HITS:
        assert looks_like_meta_narration(text), text
        hit = check_ooc(text)
        assert hit is not None, text
        assert hit.category == "meta_narration", (text, hit)


def test_mechanism_leak_is_also_capability_absence() -> None:
    """机制名词不带「工具」二字时，capability_absence 那条老判据也得接住。"""
    from gsuid_core.ai_core.agent_run.speech_policy import looks_like_capability_absence

    text = "内部库没你的分值"
    assert not looks_like_capability_absence(text), "老判据只认工具/接口字面量"
    hit = check_ooc(text)
    assert hit is not None and hit.category in ("meta_narration", "capability_absence"), hit
    # 直接按能力缺失放行（关掉 meta_narration）时也要能拦住
    assert "capability_absence" in NEVER_RELEASE_CATEGORIES


def test_clause_granularity_and_normalization() -> None:
    """小句粒度同 _self_bound_model_leak：跨小句拼不算，词内插空格仍算。"""
    assert not looks_like_meta_narration("我翻了翻你的角色箱。系统日志里倒是挺全的。")
    assert not looks_like_meta_narration("系统，日志，都翻过了。")
    assert looks_like_meta_narration("系统 日志 没 有。")
    assert looks_like_meta_narration("内部 库 没 你的 分值")


def test_benign_in_character_lines_are_not_caught() -> None:
    """人格中性反例：正常人在世界里翻找、提到容器词，都不该被拦（AGENTS §1.9）。"""
    benign = (
        "我翻了翻你的角色箱，那件外套还在。",
        "记录我记着呢，要哪段再喊我。",
        "翻不到卷轴…先睡了",
        "库存库里没货了，去别家看看吧",
        "书库今天闭馆",
        "这个月的打卡记录没有，补不补",
        "上下文里没提到这事，你自己翻。",
        "程序跑完了，结果是 42，你要的数据在楼上",
        "听说新模型上下文窗口有一百万 token，训练数据全是合成的",
        "我没接口文档，先发「图片」我看看。",
        "那个数字我记不清了。",
        "我忘了，真的。",
    )
    for text in benign:
        assert check_ooc(text) is None, text


def test_negation_inside_a_word_is_not_an_absence_signal() -> None:
    """缺失谓词必须**贴住**机制名词：裸否定词会把正常话整句拖下水。

    旧判据是裸子串 ``(没|没有|无|未|不|别|甭|并非|并不|不存在)``，于是「不错」「不好意思」
    「无所谓」里的字都算缺失证据，而这些句子恰好又带机制名词 + 自指内部机制
    ——meta_narration 是**永不放行**类目，正常话被它拦下等于人格被强行改写。
    """
    benign = (
        # 否定词在词内
        "系统日志不错",
        "我这边记录不好意思再提了",
        "未来内部数据库会更好",
        "我这边记录无所谓",
        "我这边数据没问题",
        "服务器缓存不稳，等会儿再试",
        "系统日志未来会补",
        "系统日志别人看过了",
        "内部索引无所谓",
        "听说服务器日志不错，缓存也很稳",
        # 「不」只接不完整族后缀，不吃「不有趣/不到一百行/不着急/不存放/不留情/不全是错的」
        "系统日志不有趣",
        "系统日志不到一百行",
        "我这边记录不着急",
        "服务器缓存不存放图片",
        "系统日志不留情",
        "系统日志不全是错的",
        # 「没有」是「没+有」的子串，"没有问题""没有异常"里都有它
        "系统日志没有问题",
        "我这边记录没有问题",
        "我这边数据没有异常",
        "知识库没有问题",
        # 否定在**相邻小句**，说的不是机制
        "系统日志不错，没吃饭",
        "系统缓存很好，没有问题",
        "我这边记录不错，没什么要补充的",
        "我今天数据库连不上，你昨天说的那个我没查到",
        "别人的记录我看不到",
        "我叫不了那个内部名字",
    )
    for text in benign:
        assert not looks_like_meta_narration(text), text
        assert check_ooc(text) is None, text


def test_absence_signal_still_has_to_touch_the_mechanism_noun() -> None:
    """收紧后不能把真泄漏一起放过去（含「不完整」族与本轮回执族）。"""
    leaks = (
        "内部库没你的分值",
        "系统日志没有这一轮",
        "内部索引里没有这个人",
        "内部记录不太全",  # 「不太全」不是裸「不」
        "我后台缓存是空的",
        "我手里没有现成的数据，翻不到走势",
        "我这边记录没留底",  # 收紧「没有」之后，「没+动词」族仍要拦
        "内部数据没保存",
        "系统日志没记全",
    )
    for text in leaks:
        assert looks_like_meta_narration(text), text
        assert check_ooc(text) is not None, text
    # 这句由 mechanism_absence 接住：对用户念出「知识库」本身就是泄漏
    assert check_ooc("知识库的书名我没记全") is not None


def test_negation_in_a_neighbouring_clause_is_not_about_the_mechanism() -> None:
    """取舍的显式锁：跨小句的否定**不**算机制缺失。

    中文里「否定属于前一小句的哪个对象」在分词层面不可判——桥接会让
    「系统日志不错，没吃饭」这种正常话被永不放行类目拦下，代价远大于收益。
    「我查了内部记录，没查到」因此漏给提示词契约处理。
    """
    assert not looks_like_meta_narration("我查了内部记录，没查到")
    hit = check_ooc("我查了内部记录，没查到")
    assert hit is None, hit


def test_capability_absence_survives_a_word_between_negation_and_tool() -> None:
    """「没有天气接口」曾整句漏过（否定与「接口」之间夹了领域词就贴不上）。

    eval 实发过这句：模型讲完「没有天气接口」还接着叫人装 App，等于把能力清单
    当台词讲给群里。老判据只认「没接口」贴字。
    """
    from gsuid_core.ai_core.agent_run.speech_policy import looks_like_capability_absence

    for text in ("没有天气接口", "我这边没有天气接口", "唔…查不了。没有天气接口，唔。"):
        assert looks_like_capability_absence(text), text
        hit = check_ooc(text)
        assert hit is not None and hit.category == "capability_absence", (text, hit)
    # 反向：文档/说明/手册不是能力缺失
    for text in ("我没接口文档", "接口文档发我一份", "工具人没来开会"):
        assert not looks_like_capability_absence(text), text


def test_capability_absence_does_not_bridge_across_a_comma() -> None:
    """夹字段不得跨句读：否定的宾语在另一小句时不算「缺能力」。

    裸 ``.`` 会把「我没说完，你用接口吧」读成「没…接口」——而 capability_absence
    是永不放行类目，正常台词被拦下等于人格被整段 canned 覆盖。与 meta_narration
    侧同一条「缺失谓词必须同小句」的规则对齐（见 ``output_firewall``）。
    """
    from gsuid_core.ai_core.agent_run.speech_policy import looks_like_capability_absence

    for text in (
        "我没说完，你用接口吧",
        "没吃饭，先用接口顶着",
        "没有下雨，接口留着",
        "没问题，我用接口接一下",
    ):
        assert not looks_like_capability_absence(text), text
        assert check_ooc(text) is None, text


def test_retrieval_vocabulary_is_never_produced_by_the_framework() -> None:
    """C-2：空结果回执不得再带工具名 / 检索术语。"""
    from gsuid_core.ai_core.cognition.facade import render_cognition_block

    block = render_cognition_block("竖图偏好", [])
    assert len(block.splitlines()) == 1, block
    assert len(block) < 160, f"{len(block)} 字：{block}"
    for banned in (
        "无命中",
        "召回",
        "没存过",
        "search_cognition",
        "web_search_tool",
        "find_tools",
    ):
        assert banned not in block, f"空结果回执仍带检索语汇/工具名：{banned} -> {block}"
    assert "没有可用材料" in block


def test_prompt_stops_prescribing_a_fixed_failure_phrase() -> None:
    """C-1：宪法不再规定「失败只说没查到 / 用口吻表示此刻翻不到」。"""
    from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS

    for banned in ("只说没查到", "此刻翻不到", "翻不到"):
        assert banned not in SYSTEM_CONSTRAINTS, f"prompt 仍在规定固定失败说法：{banned}"
    for term in ("召回", "无命中", "内部", "数据库"):
        assert term in SYSTEM_CONSTRAINTS, f"机器腔禁词表缺「{term}」"
    # 「库」连着车库/书库/库存一起禁，「分值」是某业务域的词：禁词表吃日常词和
    # 业务词都是人格/能力锁定（AGENTS §1.9），这两条真泄漏交给结构判据拦。
    for term in ("、库", "分值"):
        assert term not in SYSTEM_CONSTRAINTS, f"禁词表不该收「{term}」：要么过宽、要么是业务词"
    assert len(SYSTEM_CONSTRAINTS) <= 1600, len(SYSTEM_CONSTRAINTS)


def test_find_tools_miss_is_capability_shaped_not_data_shaped() -> None:
    """C-3：未命中说的是「没有对口能力」，不是「你要的资料不存在」。"""
    src = (
        Path(__file__).resolve().parent.parent / "gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py"
    ).read_text(encoding="utf-8")
    assert "本次没有对口的专用能力" in src
    assert "未检索到与" not in src, "未命中回执仍是检索语汇"
    assert "禁止说成「你要的东西不存在」" in src


def test_answer_contract_forbids_defining_the_shape_of_an_absence() -> None:
    """C-6：认「不知道」只给结论，不解释这份「不知道」的性质/来源/范围。"""
    from gsuid_core.ai_core.persona.chat_style import ANSWER_CONTRACT

    assert "记不清就直说记不清" in ANSWER_CONTRACT
    assert "别紧接着说明这份不知道的性质、来源或范围" in ANSWER_CONTRACT
    banned = ("唔", "呼", "zzz", "早柚", "貉", "卷轴", "分值", "没查到")
    assert not any(w in ANSWER_CONTRACT for w in banned), ANSWER_CONTRACT


def test_leak_lines_1_and_3_are_deprived_of_their_register() -> None:
    """C-5 取舍的显式锁：第 1、3 条闸不拦，靠「框架不再生产该语汇」根治。

    拦它们需要「没查到 / 翻页」这类词表，而同形的「翻不到卷轴…先睡了」是人话
    （``test_benign_fp`` 已锁为放行）。AGENTS §1.9 禁止用词表判风格，故此处断言
    闸**不**命中，同时断言框架三个来源都不再产出这套口径。
    """
    from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS
    from gsuid_core.ai_core.cognition.facade import render_cognition_block
    from gsuid_core.ai_core.persona.chat_style import ANSWER_CONTRACT

    assert check_ooc(LEAK_1) is None
    assert check_ooc(LEAK_3) is None
    sources = render_cognition_block("原话", []) + SYSTEM_CONSTRAINTS + ANSWER_CONTRACT
    for banned in ("没查到", "没翻到", "只翻了你那", "不归我翻"):
        assert banned not in sources, f"框架仍在生产检索腔：{banned}"
