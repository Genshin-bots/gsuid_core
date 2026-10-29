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
