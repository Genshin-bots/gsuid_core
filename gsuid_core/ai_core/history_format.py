"""
消息历史的 AI 格式化工具

将 gsuid_core.message_history 中的通用消息记录（MessageRecord）转换为
AI 可用的 prompt / messages / Agent 上下文格式。

本模块依赖通用消息历史模块，方向为 ai_core -> message_history，
通用消息历史模块本身不感知这些 AI 格式化逻辑。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional
from datetime import datetime

from gsuid_core.message_history import MessageRecord


def history_to_prompt(
    history: List[MessageRecord],
    include_system: bool = True,
    format_template: Optional[str] = None,
) -> str:
    """
    将历史记录转换为AI可用的prompt字符串

    Args:
        history: 消息记录列表
        include_system: 是否包含system消息
        format_template: 自定义格式模板，默认使用标准格式
            模板变量: {role}, {content}, {timestamp}, {index}, {user_id}, {user_name}

    Returns:
        格式化后的prompt字符串

    Example:
        >>> history = manager.get_history(event)
        >>> prompt = history_to_prompt(history)
        >>> # 输出格式:
        >>> # [用户-123]: 你好
        >>> # [AI]: 你好！有什么可以帮助你的吗？
    """
    if not history:
        return ""

    if format_template:
        lines = []
        for i, record in enumerate(history, 1):
            if record.role == "system" and not include_system:
                continue
            line = format_template.format(
                role=record.role,
                content=record.content,
                timestamp=record.timestamp,
                index=i,
                user_id=record.user_id,
                user_name=record.user_name or "",
            )
            lines.append(line)
        return str("\n".join(lines))

    # 默认格式
    role_display = {
        "user": "[用户",
        "assistant": "[AI]",
        "system": "[系统]",
    }

    lines = []
    for record in history:
        if record.role == "system" and not include_system:
            continue

        if record.role == "user":
            user_label = record.user_name or record.user_id
            lines.append(f"[用户-{user_label}]: {record.content}")
        else:
            role_label = role_display.get(record.role, f"[{record.role}]")
            lines.append(f"{role_label}: {record.content}")

    return str("\n".join(lines))


def history_to_messages(
    history: List[MessageRecord],
    include_system: bool = True,
) -> List[Dict[str, str]]:
    """
    将历史记录转换为OpenAI格式的messages列表

    Args:
        history: 消息记录列表
        include_system: 是否包含system消息

    Returns:
        OpenAI格式的messages列表

    Example:
        >>> history = manager.get_history(event)
        >>> messages = history_to_messages(history)
        >>> # 输出: [{"role": "user", "content": "你好"}, ...]
    """
    messages = []

    for record in history:
        if record.role == "system" and not include_system:
            continue

        messages.append(
            {
                "role": record.role,
                "content": record.content,
            }
        )

    return messages


def _format_timestamp(ts: float, ref_ts: Optional[float] = None) -> str:
    """
    将 Unix 时间戳格式化为对模型友好的时间字符串。

    策略（以 ref_ts 为"当前时间"基准，默认用 time.time()）：
        - 今天内         → "HH:MM"
        - 昨天           → "昨天 HH:MM"
        - 今年内（非昨天）→ "M月D日 HH:MM"
        - 跨年           → "YYYY年M月D日 HH:MM"

    Args:
        ts: 消息的 Unix 时间戳
        ref_ts: 参照时间戳，默认为当前时间
    """
    if ref_ts is None:
        ref_ts = time.time()

    msg_dt = datetime.fromtimestamp(ts)
    ref_dt = datetime.fromtimestamp(ref_ts)

    msg_date = msg_dt.date()
    ref_date = ref_dt.date()
    delta_days = (ref_date - msg_date).days

    time_str = msg_dt.strftime("%H:%M")

    if delta_days == 0:
        return time_str
    elif delta_days == 1:
        return f"昨天 {time_str}"
    elif msg_dt.year == ref_dt.year:
        return f"{msg_dt.month}月{msg_dt.day}日 {time_str}"
    else:
        return f"{msg_dt.year}年{msg_dt.month}月{msg_dt.day}日 {time_str}"


# 同一用户连发多段消息的合并窗口（秒）：窗口内的相邻同人消息在历史里合并为
# 一个发言块，让"@某人"+"醒了吗"这类拆条连发对模型呈现为一句完整的话。
# 窗口值唯一来源是 ai_config `history_merge_window`（可在线调）。
def _merge_window() -> float:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return float(ai_config.get_config("history_merge_window").data)


def format_history_for_agent(
    history: List[MessageRecord],
    current_user_id: Optional[str] = None,
    current_user_name: Optional[str] = None,
) -> str:
    """
    将历史记录格式化为 Agent 可用的上下文格式。

    输出结构：
        当前用户ID: {id}({name}) [{HH:MM}]：   ← 最新一条触发消息
        "{content}"
        --- 附加元数据 ———

        【历史对话】
        {user_id}({name}) [昨天 HH:MM]：
        "{content}"

        AI [HH:MM]：
        "{content}"

    特殊处理：
        - 时间戳智能格式化：今天只显示 HH:MM，昨天/跨日/跨年逐级补全
        - user_name 有值时以 id(name) 形式显示，帮助模型关联用户
        - AI 回复中的 @数字 保持原样透传，让模型感知自己之前的 @ 行为
        - 跳过 role=system 的记录
        - current_user_id 的最后一条 user 消息作为"当前消息"置于历史之前
        - at_list 渲染为「@了用户 xxx——@的是TA不是你」：入库层（handler.msg_process）
          已把 @Bot 自己转成 is_tome、不进 at_list，故历史里的 @ 一定指向别人，
          可放心标注，防止模型把"@某人 + 醒了吗"误认为在叫自己
        - 同一用户在合并窗口（ai_config `history_merge_window`）内连发的相邻消息
          合并为一个发言块，帮助模型把拆条连发读成一句完整的话

    Args:
        history: 消息记录列表（时间正序）
        current_user_id: 当前触发 AI 的用户 ID
        current_user_name: 当前触发 AI 的用户昵称（可选，用于当前消息标签）

    Returns:
        格式化后的上下文字符串

    Example:
        >>> context = format_history_for_agent(history, current_user_id="456", current_user_name="小明")
        >>> # 当前用户ID: 456(小明) [14:32]：
        >>> # "今天天气怎么样？"
        >>> #
        >>> # 【历史对话】
        >>> # 456(小明) [昨天 22:10]：
        >>> # "你好"
        >>> #
        >>> # AI [昨天 22:10]：
        >>> # "唔…你好。"
        >>> #
        >>> # 789 [3月12日 09:05]：
        >>> # "大家好"
    """
    if not history:
        return ""

    # 以当前时间为基准做时间格式化（整个函数调用期间固定，避免跨秒漂移）
    ref_ts = time.time()

    # ----------------------------------------------------------------
    # 1. 找出"当前消息"：current_user_id 在 history 中最后一条 user 记录
    # ----------------------------------------------------------------
    current_record_index: Optional[int] = None
    if current_user_id is not None:
        for i in range(len(history) - 1, -1, -1):
            r = history[i]
            if r.role == "user" and r.user_id == current_user_id:
                current_record_index = i
                break

    # ----------------------------------------------------------------
    # 2. 构建用户标签：id(name) 或 id（无昵称时省略括号）
    # ----------------------------------------------------------------
    def _user_label(user_id: str, user_name: Optional[str]) -> str:
        if user_name:
            return f"{user_id}({user_name})"
        return user_id

    # ----------------------------------------------------------------
    # 2b. 昵称解析表：user_id → 最近一次出现的昵称（供 @目标 显示人话名字）
    # ----------------------------------------------------------------
    name_map: Dict[str, str] = {}
    for r in history:
        if r.role == "user" and r.user_name:
            name_map[str(r.user_id)] = r.user_name

    # ----------------------------------------------------------------
    # 3. 格式化单条记录为文本块
    # ----------------------------------------------------------------
    def _record_body(record: MessageRecord) -> List[str]:
        """单条记录的正文（不含说话人头行），供独立/合并两种渲染复用。"""
        block: List[str] = []

        content = record.content.strip()
        if content:
            block.append(f'"{content}"')

        metadata = record.metadata or {}

        # @用户列表：入库层已保证 @Bot 自己不进 at_list（转成 is_tome），
        # 历史里的 @ 必然指向别的用户——显式标注，防止"@某人+醒了吗"被误读成叫自己。
        # 标注文案唯一定义在 interaction_scaffold（C-3 寻址门按字面匹配它）
        from gsuid_core.ai_core.interaction_scaffold import AT_OTHER_MARKER

        for at_id in metadata.get("at_list", []):
            at_key = str(at_id)
            at_label = _user_label(at_key, name_map[at_key] if at_key in name_map else None)
            block.append(f"--- @了用户: {at_label}{AT_OTHER_MARKER} ———")

        # 单张图片
        image_id = metadata.get("image_id")
        if image_id:
            block.append(f"--- 用户上传图片ID: {image_id} ———")

        # 多张图片
        for img_id in metadata.get("image_id_list", []):
            block.append(f"--- 用户上传图片ID: {img_id} ———")

        # 音频ID
        audio_id = metadata.get("audio_id")
        if audio_id:
            block.append(f"--- 用户上传音频ID: {audio_id} ———")

        # 文件ID
        file_id = metadata.get("file_id")
        if file_id:
            block.append(f"--- 用户上传文件ID: {file_id} ———")

        return block

    def _render_block(records: List[MessageRecord], label: str) -> List[str]:
        """一组（≥1 条）记录渲染为一个发言块：头行（首条时间戳）+ 逐条正文 + 空行。"""
        block = [f"{label} [{_format_timestamp(records[0].timestamp, ref_ts)}]："]
        for rec in records:
            block.extend(_record_body(rec))
        block.append("")
        return block

    # ----------------------------------------------------------------
    # 4. 组装输出
    # ----------------------------------------------------------------
    output: List[str] = []

    # 4a. 当前消息（置于最前，不进入历史对话块）
    if current_record_index is not None and current_user_id:
        current_record = history[current_record_index]
        # current_user_name 优先用传入参数，其次用 record 自带的
        name = current_user_name or current_record.user_name
        base_label = _user_label(current_user_id, name)
        label = f"当前用户ID: {base_label}"
        output.extend(_render_block([current_record], label))

    # 4b. 历史对话分隔线 + 其余记录。
    # 同一用户在合并窗口内的相邻消息合并成一个发言块（拆条连发 → 一句完整的话）。
    def _make_label(record: MessageRecord) -> str:
        if record.role == "assistant":
            # Fix-04: AI 回复增加回复对象标签
            reply_to = None
            reply_name = None
            if record.metadata:
                reply_to = record.metadata.get("reply_to_user_id")
                reply_name = record.metadata.get("reply_to_user_name")
            if reply_to:
                target = f"{reply_to}({reply_name})" if reply_name else reply_to
                return f"AI→{target}"
            return "AI"
        return _user_label(record.user_id, record.user_name)

    history_lines: List[str] = []
    pending_group: List[MessageRecord] = []  # 当前累积的同人连发消息组
    merge_window = _merge_window()

    def _flush_group() -> None:
        if not pending_group:
            return
        history_lines.extend(_render_block(pending_group, _make_label(pending_group[0])))
        pending_group.clear()

    for i, record in enumerate(history):
        if record.role == "system":
            continue
        if i == current_record_index:
            continue

        # 窗口锚定组内**首条**而非前一条：链式相邻比较会让"每分钟一条"的长独白无限合并成
        # 一个只有首条时间戳的巨块，heartbeat 会把最新消息误判成半小时前说的。
        if (
            pending_group
            and record.role == "user"
            and pending_group[-1].role == "user"
            and str(record.user_id) == str(pending_group[-1].user_id)
            and 0 <= record.timestamp - pending_group[0].timestamp <= merge_window
        ):
            pending_group.append(record)
            continue

        _flush_group()
        pending_group.append(record)

    _flush_group()

    if history_lines:
        output.append("【历史对话】")
        output.extend(history_lines)

    return "\n".join(output)
