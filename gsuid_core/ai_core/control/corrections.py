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


def missing_offered_tool_directive(*, tool_pool_size: int) -> Directive:
    """声称没有工具，但本轮 schema 已有可用工具。"""
    return Directive(
        kind="correction",
        reason_code="missing_offered_tool",
        observation=(
            "你声称没有对应工具，但本轮主会话已经装配了查询/修改/取消类工具。"
            "先用列表类工具定位目标，再修改或取消。"
            "禁止再说没有工具，禁止为此 create_subagent。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail=f"可用工具 {tool_pool_size} 个"),
    )


def addressed_silence_directive() -> Directive:
    """被呼叫却整段沉默。"""
    return Directive(
        kind="correction",
        reason_code="addressed_silence",
        observation=("本轮你被直接呼叫，却只输出了沉默。该拒就短拒，该办事就调工具，该闲聊就短回；不要只沉默。"),
        obligations=(
            Obligation(
                must="deliver",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0),
    )


def cover_hit_zero_tool_directive() -> Directive:
    """用户原话命中已注册工具 cover，却零调用空口作答。"""
    return Directive(
        kind="correction",
        reason_code="cover_hit_zero_tool",
        observation=(
            "用户原话命中了已注册工具的覆盖句，但本轮没有调用任何工具，刚才的答案是空口编的。"
            "现在调用 find_tools 或对口工具。禁止用 dispute_directive 把空口建议留住。"
            "不要重复刚才那句。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail="原话命中工具 cover"),
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


def framework_idle_deliver_directive() -> Directive:
    """交付回灌零工具却对用户报进度。纠正去出图/发图，进度句不放行。"""
    return Directive(
        kind="correction",
        reason_code="framework_idle_deliver",
        observation=(
            "这是任务交付回灌，不是用户在催进度。上一条进度话用户看不到。"
            "有图就 send_message_by_ai 发给发起人；长事实包只可 "
            'create_subagent(agent_profile="render_agent")。'
            "不要新开查询。对用户只输出 <SILENCE>。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail="交付回灌零工具"),
    )


def master_title_directive(title: str) -> Directive:
    """非主人收件人的台词里出现了主人称呼。改写后再发，不许原样放行。"""
    return Directive(
        kind="correction",
        reason_code="master_title",
        observation=(
            f"接收人不是主人，上一段含有「{title}」，用户看不到。"
            "改写成不含这个称呼的一句再发给用户。不要只输出 <SILENCE>。"
        ),
        obligations=(
            Obligation(
                must="deliver",
                satisfied_by=("user_visible_sent",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail="主人称呼"),
    )


def entity_zero_tool_directive() -> Directive:
    """点名提问已装上查询工具，却零调用作答。"""
    return Directive(
        kind="correction",
        reason_code="entity_without_tool",
        observation=(
            "本轮已经为这句装上了查询工具，但没有调用任何工具就回答了。"
            "先调用能回答这句的查询工具，再按工具结果用角色口吻回答。"
            "若这句只是闲聊、不需要查数据，调用 dispute_directive 申辩。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                satisfied_by=("any_tool_called",),
            ),
        ),
        evidence=Evidence(tool_calls=0),
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


def premature_claim_directive() -> Directive:
    """完成态被拦、图还没发出。纠正改口，不许再宣称完成，也不出图。"""
    return Directive(
        kind="correction",
        reason_code="premature_delivery",
        observation=(
            "你上一段被拦下了，用户没看到。图还没发出，回复却是完成态。"
            "用当前人格改成一句短话，不要宣称已经做好或已经发出。"
            "不要委派出图，不要只输出 <SILENCE>。"
        ),
        obligations=(
            Obligation(
                must="deliver",
                satisfied_by=("user_visible_sent",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail="出站话术闸拦下了完成态"),
    )


def blocked_voice_directive() -> Directive:
    """过程词或编排词被拦，用户没看到。纠正成角色短句，不出图。"""
    return Directive(
        kind="correction",
        reason_code="blocked_voice",
        observation=(
            "你上一段被拦下了，用户没看到：过程说明或内部编排不能当对用户的话。"
            "用当前人格改成一句短话再说。"
            "不要委派出图，不要只输出 <SILENCE>。"
        ),
        obligations=(
            Obligation(
                must="deliver",
                satisfied_by=("user_visible_sent",),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail="出站话术闸拦下了过程或编排"),
    )


def numeric_recitation_directive() -> Directive:
    """念数被拦、用户没看到。纠正只许改委派出图，不许再念进气泡。"""
    return Directive(
        kind="correction",
        reason_code="numeric_recitation",
        observation=(
            "你上一段给用户的回复被拦下了，没有发出去：多点数字对照不能当群聊台词。"
            '把那段改委派 create_subagent(agent_profile="render_agent") 出图。'
            "不要再把数字念进气泡，不要只输出 <SILENCE>。"
        ),
        obligations=(
            Obligation(
                must="call_tool",
                tool_name="create_subagent",
                tool_args_match={"agent_profile": "render_agent"},
                satisfied_by=("render_delegated", "image_sent"),
            ),
        ),
        evidence=Evidence(tool_calls=0, detail="出站话术闸拦下了念数"),
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
