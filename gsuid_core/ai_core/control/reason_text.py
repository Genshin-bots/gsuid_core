"""原因码在写入会话日志时冻结的中文说明。

改判定之后，旧日志仍显示写下时的这句，不跟随后来的代码。
"""

from __future__ import annotations

# 同一原因码在拦住和纠正里共用一句。条目类型已经区分「没发出」和「要求重说」。
_REASON_TEXT: dict[str, str] = {
    "master_title": "接收人不是主人，台词里出现了主人称呼。",
    "orchestration_leak": "台词向用户讲了内部委派或节点名。",
    "delivery_narration": "同一句里既播报已经交付，又说不必再回复。",
    "report_speech": "台词是长标题、多段或表格，本该出图。",
    "silence_only_or_async": "任务还在进行，这句不是允许的那一次接话。",
    "framework_nudge_no_speech": "这是纠正轮，这句不是一句等待。",
    "process_meta": "台词对用户讲了框架过程或内部字段。",
    "premature_delivery": "还没查到、也没发图，台词却说已经做好。",
    "numeric_recitation": "待出图的材料被念成了多点数字。",
    "empty_handoff": "已有材料，台词却推说不念、不出图。",
    "entity_without_tool": "这轮点到了实体，还没调用查询工具就回答。",
    "deliver_before_send": "交付回灌里的进度句或大段表格，还没先发出去。",
    "status_without_tool": "用户在追问进度，还没查状态就报了结果。",
    "empty": "台词是空白。",
    "output_gate_fuse": "出站闸连续打回，这轮熔断。",
    "output_gate_angle_bracket": "台词含有非法尖括号。",
    "output_gate_ooc": "台词出戏，出站闸打回。",
    "output_gate_inner_os": "台词是内心旁白。",
    "output_gate_rewrite": "出站闸打回原文。",
    "output_gate_fallback": "出站闸改写或清空了原文。",
    "output_gate_tool": "发送工具或开场句被出站闸拦住。",
    "fake_done_withheld": "还没有做成功，台词却声称做完。",
    "main_channel_cap": "这一轮可见台词已经到条数上限。",
    "ooc_recover": "出戏改口之后没有可发的话。",
    "framework_leak_stripped": "去掉框架泄漏之后没有可发的话。",
    "fake_done": "上一句声称做完，但没有成功的改动。",
    "missing_offered_tool": "该用的工具一个都没调用。",
    "addressed_silence": "有人直接点名，回复却是沉默。",
    "cover_hit_zero_tool": "点到了能力，却没有调用对应工具。",
    "structural_zero_tool": "这轮该用工具，却一个都没调用。",
    "framework_idle_deliver": "交付唤醒后空口报了进度或交了长报告。",
    "blocked_voice": "上一句是编排、过程旁白或交付播报。",
    "render_pending": "材料还没出图。",
    "model_voluntary": "模型自己选择沉默。",
    "empty_output": "这一轮没有产出正文。",
    "delegated_render_unfinished": "出图还没完成。",
    "usage_limit_no_material": "用量到顶，且没有可交代的材料。",
    "usage_limit_fallback_silence": "用量到顶后的补答不可发。",
    "no_material_reply_silence": "没有材料可答。",
    "already_sent_by_tool": "发送工具已经把话发给用户了，这句复述不再发。",
}


def reason_text(reason: str) -> str:
    """把原因码译成写下当时的中文。多个码用逗号分隔，未知码保留原样。"""
    parts = [part.strip() for part in reason.split(",") if part.strip()]
    lines: list[str] = []
    for part in parts:
        if part in _REASON_TEXT:
            lines.append(_REASON_TEXT[part])
        else:
            lines.append(f"未登记原因：{part}")
    return "；".join(lines)
