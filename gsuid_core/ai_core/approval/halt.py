"""运行器用来停机的标记。不是人格句子，不进 control/lines.py。"""

APPROVAL_HALT_MARK = "[[APPROVAL_HALT]]"
# 回调已另开下一张票：裁决函数看到这个开头就不回灌。
APPROVAL_HOLD_MARK = "[[APPROVAL_HOLD]]"
DEFAULT_APPROVAL_TTL = 600


def text_is_approval_halt(text: str) -> bool:
    return text.startswith(APPROVAL_HALT_MARK)


def action_failed(text: str) -> bool:
    """领域回调或续作正文表示这一步没有做成。"""
    body = text.strip()
    if body.startswith(("安装失败", "❌", "🚫")):
        return True
    return body.startswith("⚠️ 能力代理执行失败")


def halt_line(short_id: str, *, delivered: bool, reused: bool) -> str:
    """工具返回值。不含确认码。运行器见到开头标记就结束本轮。"""
    if reused:
        note = "仍在等原先那条确认，没有新开审批。"
    elif delivered:
        note = "确认码已私聊送出。"
    else:
        note = "私聊没发出，请到审批中心处理。"
    return f"{APPROVAL_HALT_MARK}\n已挂起审批 #{short_id}。{note}不要再安装，不要再派开发任务。"
