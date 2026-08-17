"""settle 纠正指令的构造器（观察 + 凭据 + 可申辩义务）。

每条纠正都遵守两条规则：

* ``observation`` 只陈述框架结构上**确知**的事实（零工具调用、工具返回条数、
  是否已委派出图），不做「你在念表」这类文风断言之外的推测。
* 义务恒 ``disputable``：观察若与事实不符，模型走 ``dispute_directive`` 申辩，
  原答案照原样交付。旧版把「立即照做」与「只可 <SILENCE>」并列成许可集，
  观察为假时模型只能选空操作 → 整轮零输出（生产活锁）。
"""

from __future__ import annotations

from gsuid_core.ai_core.control.directive import Evidence, Directive, Obligation


def fake_done_directive(*, tool_pool_size: int) -> Directive:
    """声称已办完却零工具调用。"""
    return Directive(
        kind="correction",
        reason_code="fake_done",
        observation=(
            "你上一条回复声称已完成某个操作，但本轮没有任何工具调用记录——"
            "该声明没有执行支撑。现在真正调用对应工具执行；"
            "若确实做不到，就如实告诉用户「刚才说错了，还没有做」。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail=f"可用工具 {tool_pool_size} 个"),
    )


def structural_zero_tool_directive(*, tool_pool_size: int) -> Directive:
    """未读附件或可继承跟进 + 工具池非空 + 零调用。"""
    return Directive(
        kind="correction",
        reason_code="structural_zero_tool",
        observation=(
            "本轮有未处理的附件，或可继承的上轮工具任务，但没有调用任何工具。"
            "现在调用对应工具；缺参数先用上文实体试一次。"
            "若附件或跟进并不需要工具，调用 dispute_directive 申辩，不要重复原答。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail=f"可用工具 {tool_pool_size} 个"),
    )


def status_zero_tool_directive() -> Directive:
    """用户追问进度，但零查询工具就报了状态。"""
    return Directive(
        kind="correction",
        reason_code="status_without_tool",
        observation=(
            "用户在追问进行中事项的进度，但你本轮没有调用任何查询工具就报了状态。"
            "先核实真实状态（在途委派用 check_delegation，看板用 list_my_kanban_tasks，"
            "产物用 artifact_get_recent），再用角色短句说明还在弄/弄好了/翻砸了。"
            "不要空口说「快好了」。"
        ),
        obligations=(
            Obligation(
                must="check_delegation",
                satisfied_by=("status_tool_called", "delegation_checked"),
            ),
        ),
        evidence=Evidence(tool_calls=0),
    )


def render_obligation_directive(*, recited_report: bool, tool_calls: int) -> Directive:
    """真把长结构当台词念出来时，才建议改出图。短答不纠。"""
    observation = "本轮工具返回里有较长结构，你把它整段念出来了。" if recited_report else "本轮工具返回里有较长结构。"
    return Directive(
        kind="correction",
        reason_code="report_speech" if recited_report else "render_pending",
        observation=(
            observation + "一两句能说清就保持原答或申辩；"
            "只有对照/多日/多项才值得委派 render_agent 出图，不要自己写 HTML。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                tool_name="create_subagent",
                tool_args_match={"agent_profile": "render_agent"},
                satisfied_by=("render_delegated", "image_sent"),
            ),
        ),
        evidence=Evidence(tool_calls=tool_calls, structured_returns=1),
    )
