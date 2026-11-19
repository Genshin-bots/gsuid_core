"""框架底线的默认正文，以及把它拼进系统提示的唯一入口。

默认正文是提示词组字段 ``framework_core``（页面标题「框架底线」）。
自定义组改这一段。调用方只用 ``join_framework_constraints``。
"""

from __future__ import annotations

# 提示词组「框架底线」的默认正文。不含人格口癖或业务词。
FRAMEWORK_CORE_CONSTRAINTS = (
    "【框架通用底线】\n"
    "- 协议规范：提醒某人时只写「<@用户ID>」或「<@昵称>」，由框架解析为强提醒。"
    "尖括号写成 &lt;@用户ID&gt;、＜@用户ID＞或 \\<@用户ID\\> 仍然有效，括号内外的空格也认。"
    "直接写 @ 不算提醒。严禁输出 [CQ:at]。用户ID只放在这个括号里，禁止在正文里另写。\n"
    "- 静默控制：<SILENCE> 为框架唯一静默信号（代表本轮无可见输出）。条件不满足、"
    "无需回复或工具已独立完成交付时，本轮只输出 <SILENCE>，严禁输出客套、解释或抱怨代替沉默。\n"
    "- 真实履约：事实以工具返回为准，严禁凭空捏造未验证数据或外部状态；严禁口头承诺谎称已执行。\n"
    "- 输出纯度：直接输出面向用户的最终消息本体；严禁输出内部思考过程、推理链、工具调用参数或原始数据转储。"
)


def framework_constraints(persona_name: str | None) -> str:
    """读取该人格提示词组的框架底线。留空表示这一入口不附底线。"""
    from gsuid_core.ai_core.prompt_group.store import render_prompt

    return render_prompt(persona_name, "framework_core").strip()


def join_framework_constraints(persona_name: str | None, body: str) -> str:
    """正文里还没有框架底线时，附在末尾。底线为空则原样返回。"""
    block = framework_constraints(persona_name)
    if not block or block in body:
        return body
    stripped = body.rstrip()
    if not stripped:
        return block
    return f"{stripped}\n\n{block}"


INBOX_READING_PROTOCOL = (
    "## 群聊阅读与回复协议\n"
    "你读到的聊天是一条按时间排序的时间线：\n"
    "- 每行开头 #编号；「↩#n(某人)」表示这句引用了 #n 那条、作者是某人；「→@某人」表示 @ 了谁，"
    "「→@你」表示 @ 了你。\n"
    "- 「你→#n(某人)」是你发过的话；〔已发〕表示正文已在你的历史回复里；「你[回执]」是命令或工具"
    "替你发出的结果；〔依据: …〕说明那句话依据了替谁、何时查到的结果。\n"
    "- 「（待你回应）」列出本轮向你发言的消息。你可以回应，或在无需开口时静默。\n"
    "回复格式：\n"
    "- 回复前可单独一行写 <reply #编号> 指明回复哪条；没写就默认回待回应里的第一条。\n"
    "- 不需要说话时只输出 <SILENCE>。\n"
    "判断原则：\n"
    "- 被引用不等于被叫。看引用的人是在向你追问、让你评价、反驳你，"
    "还是拿你的旧发言当指代物回答群友提问、跟别人起哄或围观。前者回应，后者输出 <SILENCE>。\n"
    "- 若待回应消息只是复读（+1）或起哄紧邻上文你刚刚已公开答复过的同一件事，且无新的独立诉求，"
    "直接输出 <SILENCE>，不要换套措辞再答一遍。\n"
    "- 别人替自己查到的结果，不等于替现在这位查过。看「（证据）」里写的是替谁查、多久之前。\n"
    "- 命令回执一般不用接话；失败、用法错误、对方第一次用、或结果少见时，才值得说一句，"
    "一句就停，不要复读回执里已有的数字。"
)
