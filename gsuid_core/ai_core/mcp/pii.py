"""PII / 凭据脱敏模块（模型输入闸 + 输出闸共用）。

威胁模型边界是 LLM：存储面（message_history / DB / 服务端日志）部署者
可见不管；进模型上下文与发出站消息过同一套掩码。

- ``mask_pii``：通用 PII 就地掩码（手机号/证件/地址/收货人/凭据形态）
- ``mask_credential_command``：``mcp绑定 X <tok>`` → ``mcp绑定 <凭据已隐藏>``
- ``mask_for_agent``：进模型上下文用；私聊只掩凭据形态，群聊全掩
- ``apply_context_mask``：重建 ModelRequest.parts（不原地改——与存储的
  session.history 共享 part 对象，必须 replace 出新对象）
"""

import re
from typing import Any, List
from dataclasses import replace

# ── 模式表 ──────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 银行卡 16-19 位：不与手机号/身份证重叠（先掩前两者后扫剩余）
_BANKCARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
# 详细住址：同句含 省市区县 且 路街巷道号栋室单元
_ADDR_REGION_RE = re.compile(r"[省市区县]")
_ADDR_DETAIL_RE = re.compile(r"[路街巷道号栋室单元]")
# JSON 字段名标记的地址值
_ADDR_FIELD_RE = re.compile(
    r"((?:address|addr|receiver|consignee|收货地址|配送地址|详细地址)[\"'，：:=\s]+)([^,\"'，。]{6,60})",
    re.IGNORECASE,
)
# 姓名上下文标记：收货人/收件人/联系人/姓名/realName 后的 2-4 个汉字
_NAME_FIELD_RE = re.compile(
    r"(收货人|收件人|联系人|姓名|realName)([\"'，：:=\s]+)([\u4e00-\u9fff]{2,4})",
    re.IGNORECASE,
)
# 凭据形态：Bearer xxx / token=xxx / api_key=xxx / key:xxx
_BEARER_RE = re.compile(r"Bearer\s+\S{8,}", re.IGNORECASE)
_CRED_KV_RE = re.compile(
    r"((?:token|api_?key|app_?key|secret|password|auth)[=:：]\s*)(\S{6,})",
    re.IGNORECASE,
)
# 用于「整条丢弃」判定的凭据形态（observer _gate / DM 输入闸共用）
_CRED_FORM_RE = re.compile(
    r"(?:Bearer\s+\S{8,})|(?:(?:token|api_?key|app_?key|secret|password|auth)[=:：]\s*\S{6,})",
    re.IGNORECASE,
)


def _mask_address_value(v: str) -> str:
    """字段标记的地址值：保留到区/县，其后 ***。"""
    m = re.search(r"(?:省|自治区|市|区|县)", v)
    if m is None:
        return v[:3] + "***" if len(v) > 3 else "***"
    end = m.end()
    # 继续吃紧跟的 市/区/县 组合（如「上海市黄浦区」停在最后一个区县）
    while True:
        nxt = re.match(r"[\u4e00-\u9fff]{2,}?(?:区|县|市)", v[end:])
        if nxt is None:
            break
        end += nxt.end()
    return v[:end] + "***"


def _last_admin_boundary(seg: str) -> int:
    """返回 seg 内最后一个连续行政区边界（省/市/区/县）的结束位置，无则 -1。

    「上海市黄浦区南京东路…」→ 停在「黄浦区」之后（连吃市+区）。
    """
    end = -1
    pos = 0
    for m in re.finditer(r"[\u4e00-\u9fff]{1,}?(省|自治区|市|区|县)", seg):
        # 连续行政链条：上一次边界与本段之间只隔非行政字符时仍算延伸
        if end == -1 or m.start() <= pos:
            end = m.end()
            pos = m.end()
        else:
            break
    return end


def _mask_address_segment(seg: str) -> str:
    """疑似地址句：保留到最后一个行政区边界，其后整段掩为 ***。

    例：``地址 上海市黄浦区南京东路100号3栋502室`` → ``地址 上海市黄浦区***``
    """
    end = _last_admin_boundary(seg)
    if end <= 0 or end >= len(seg):
        return seg
    # 边界后必须还有地址细节词（防「我在黄浦区上班」这类非地址误掩）
    if not _ADDR_DETAIL_RE.search(seg[end:]):
        return seg
    return seg[:end] + "***"


def _mask_addresses(text: str) -> str:
    """逐句扫描：同句含 行政区词+地址细节词 → 掩到区县。"""
    parts = re.split(r"([^\n，。；;,.]+)", text)
    out: List[str] = []
    for seg in parts:
        if _ADDR_REGION_RE.search(seg) and _ADDR_DETAIL_RE.search(seg):
            seg = _ADDR_FIELD_RE.sub(lambda fm: fm.group(1) + _mask_address_value(fm.group(2)), seg)
            seg = _mask_address_segment(seg)
        out.append(seg)
    return "".join(out)


def mask_pii(text: str) -> str:
    """通用 PII 就地掩码。返回打码后的文本。"""
    if not text:
        return text
    out = _PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], text)
    out = _IDCARD_RE.sub(lambda m: m.group(0)[:4] + "**********" + m.group(0)[-4:], out)
    out = _BANKCARD_RE.sub(lambda m: "****" + m.group(0)[-4:], out)
    out = _ADDR_FIELD_RE.sub(lambda m: m.group(1) + _mask_address_value(m.group(2)), out)
    out = _mask_addresses(out)
    out = _NAME_FIELD_RE.sub(lambda m: m.group(1) + m.group(2) + m.group(3)[0] + "*", out)
    out = _BEARER_RE.sub("Bearer ****", out)
    out = _CRED_KV_RE.sub(lambda m: m.group(1) + "****", out)
    return out


def pii_hits(text: str) -> list[str]:
    """返回命中的 PII 类型列表（审计用，不返回原文）。"""
    hits: list[str] = []
    if not text:
        return hits
    if _PHONE_RE.search(text):
        hits.append("phone")
    if _IDCARD_RE.search(text):
        hits.append("id_card")
    if _BANKCARD_RE.search(text):
        hits.append("bank_card")
    if _ADDR_FIELD_RE.search(text):
        hits.append("address_field")
    if _NAME_FIELD_RE.search(text):
        hits.append("name_field")
    if _BEARER_RE.search(text) or _CRED_KV_RE.search(text):
        hits.append("credential")
    return hits


def has_credential_form(text: str) -> bool:
    """文本是否含凭据形态（Bearer / token= / api_key= 等）。"""
    return bool(text) and bool(_CRED_FORM_RE.search(text))


def mask_credential_forms(text: str) -> str:
    """只掩凭据形态（私聊输入闸用——保留手机号等私有记忆能力）。"""
    if not text:
        return text
    out = _BEARER_RE.sub("Bearer ****", text)
    return _CRED_KV_RE.sub(lambda m: m.group(1) + "****", out)


_CRED_CMD_RE = re.compile(r"^\s*mcp(绑定|解绑|绑定列表)\b", re.IGNORECASE)


def is_credential_command(text: str) -> bool:
    """是否为凭据命令（mcp绑定/mcp解绑/mcp绑定列表 开头）。"""
    return bool(text) and bool(_CRED_CMD_RE.match(text))


def mask_credential_command(text: str) -> str:
    """凭据命令掩尾部：``mcp绑定 瑞幸 tok`` → ``mcp绑定 <凭据已隐藏>``。

    无参命令（仅动词）原样返回。多行文本逐行处理。
    """
    if not text or not is_credential_command(text):
        return text
    head, _, tail = text.strip().partition(" ")
    head2, _, tail2 = tail.partition("\n") if tail else ("", "", "")
    if not tail.strip():
        return text
    masked_head = f"{head} <凭据已隐藏>"
    return masked_head + ("\n" + tail2 if tail2 else "")


def mask_for_agent(text: str, *, is_group: bool) -> str:
    """进模型上下文的脱敏：任何 scope 掩凭据命令+凭据形态；群聊追加全 PII。"""
    if not text:
        return text
    out = mask_credential_command(text)
    out = mask_credential_forms(out)
    return mask_pii(out) if is_group else out


# ── 上下文 parts 重建（输入闸） ─────────────────────────────────────


def _mask_text_leaf(value: Any, is_group: bool) -> Any:
    if isinstance(value, str):
        return mask_for_agent(value, is_group=is_group)
    if isinstance(value, list):
        return [_mask_text_leaf(item, is_group) for item in value]
    if isinstance(value, dict):
        return {k: _mask_text_leaf(v, is_group) for k, v in value.items()}
    return value


def apply_context_mask(parts: List[Any], is_group: bool) -> List[Any]:
    """重建 ModelRequest.parts：所有文本字段过 mask_for_agent。

    - UserPromptPart.content（str 或 UserContent 列表）、ToolReturnPart/
      NativeToolReturnPart.content、TextPart.content、RetryPromptPart.content
      → 掩码副本
    - SystemPromptPart 跳过（自有注入文本，不是用户/工具数据）
    - 不原地改：parts 与 session.history 共享对象
    """
    from pydantic_ai.messages import SystemPromptPart

    masked: List[Any] = []
    for part in parts:
        if isinstance(part, SystemPromptPart):
            masked.append(part)
            continue
        content = getattr(part, "content", None)
        if content is None:
            masked.append(part)
            continue
        new_content = _mask_part_content(content, is_group)
        if new_content is content:
            masked.append(part)
            continue
        try:
            masked.append(replace(part, content=new_content))
        except TypeError:
            masked.append(part)
    return masked


def _mask_part_content(content: Any, is_group: bool) -> Any:
    """part.content 可能是 str / list[UserContent] / 其它结构。"""
    if isinstance(content, str):
        return mask_for_agent(content, is_group=is_group)
    if isinstance(content, (list, tuple)):
        return [_mask_part_content(item, is_group) for item in content]
    if isinstance(content, dict):
        return {k: _mask_part_content(v, is_group) for k, v in content.items()}
    # pydantic_ai UserContent 等 dataclass：逐字段掩文本
    if hasattr(content, "__dataclass_fields__"):
        try:
            return replace(
                content,
                **{
                    f: _mask_part_content(getattr(content, f), is_group)
                    for f in content.__dataclass_fields__
                    if isinstance(getattr(content, f, None), str)
                },
            )
        except TypeError:
            return content
    return content
