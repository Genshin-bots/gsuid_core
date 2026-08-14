"""框架→模型的控制面（唯一通道）。

历史上框架靠「往 user 槽塞文本」跟模型说话（`[用户发言]\\n（系统校验…）`），
导致模型分不清操作者与被操作者，只能在对用户可见的台词里反驳框架 → OOC。
本包把控制面收敛为带类型的 :class:`Directive`，由 ``prepare`` 渲染成
``<control>`` 信封追加到本 run request；**永不**进 user 槽、B 轨与工具检索 query。

义务（:class:`Obligation`）由 ``settle`` 结构化验证，不看模型文本；
模型认为观察不成立时走 ``dispute_directive`` 申辩，而非对用户解释。
"""

from gsuid_core.ai_core.control.directive import (
    CONTROL_ENVELOPE_TAG,
    Evidence,
    Directive,
    Obligation,
    DirectiveKind,
    ObligationKind,
    obligation_satisfied,
    render_control_envelope,
)

__all__ = [
    "CONTROL_ENVELOPE_TAG",
    "Directive",
    "DirectiveKind",
    "Evidence",
    "Obligation",
    "ObligationKind",
    "obligation_satisfied",
    "render_control_envelope",
]
