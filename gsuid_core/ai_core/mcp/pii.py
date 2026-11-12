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
from typing import List, TypeVar, Sequence
from dataclasses import replace

_PartT = TypeVar("_PartT")

# ── 模式表 ──────────────────────────────────────────────────────────

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 银行卡 16-19 位：不与手机号/身份证重叠（先掩前两者后扫剩余）
_BANKCARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_ADDR_REGION_RE = re.compile(r"[省市区县]")
_ADDR_DETAIL_RE = re.compile(r"[路街巷道号栋室单元]")
# 住宅户室级细粒度：含楼栋/单元/室/户或小区门牌，排除普通商业街牌号与旅游景点
_RESIDENTIAL_DETAIL_RE = re.compile(
    r"(?:[0-9一二三四五六七八九十百甲乙丙丁A-Za-z]+\s*(?:栋|幢|号楼|单元|室|户|弄\s*\d+\s*号))"
    r"|(?:(?:小区|公寓|花园|新村|家园|宿舍|住宅)\s*[^\n，。；;,.|<]{0,16}?[0-9一二三四五六七八九十]+\s*(?:号|栋|幢|单元|室|楼))"
)
# 显式个人地址字段（不含泛化的「地址/地点/位置」，防误伤景点与门店）
_ADDR_FIELD_RE = re.compile(
    r"((?:address|addr|receiver_?addr(?:ess)?|shipping_?addr(?:ess)?|consignee_?addr(?:ess)?|home_?addr(?:ess)?"
    r"|收货地址|配送地址|寄件地址|家庭住址|居住地址|详细住址|个人住址|户籍地址|联系地址|详细地址)"
    r"(?:[\*_~`\s]*[：:=|\"'][\*_~`\s\"']*|\s+))([^,\"'，。；;\n|<]{4,80})",
    re.IGNORECASE,
)
# 共现上下文下的泛化地址字段（同段含手机号/身份证/收货人时生效）
_ADDR_COOCCUR_FIELD_RE = re.compile(
    r"((?:地址|住址|送至|配送至)(?:[\*_~`\s]*[：:=|\"'][\*_~`\s\"']*|\s+))([^,\"'，。；;\n|<]{4,80})",
    re.IGNORECASE,
)
# 姓名上下文标记：收货人/收件人/联系人/姓名/realName 后的 2-4 个汉字（兼容 Markdown 与表格）
_NAME_FIELD_RE = re.compile(
    r"(收货人|收件人|寄件人|联系人|真实姓名|客户姓名|用户姓名|姓名|real_?name|receiver_?name|consignee_?name)"
    r"([\*_~`\s]*[：:=|\"'][\*_~`\s\"']*|\s+)([\u4e00-\u9fff]{2,4}(?:·[\u4e00-\u9fff]{2,4})?)",
    re.IGNORECASE,
)
_NON_ADMIN_SUFFIX_RE = re.compile(
    r"(?:所在|所属)?(?:地区|省市区|市区|城区|小区|社区|园区|校区|学区|景区|展区|街区|商区|城市)$"
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
_HTML_PROTECT_RE = re.compile(
    r"(<(?:style|script)\b[^>]*>.*?</(?:style|script)>|<!--.*?-->)", re.IGNORECASE | re.DOTALL
)
_HTML_CROSS_NAME_RE = re.compile(
    r"((?:收货人|收件人|寄件人|联系人|真实姓名|客户姓名|用户姓名|姓名|real_?name|receiver_?name|consignee_?name)"
    r"\s*[:：]?\s*(?:</[a-zA-Z0-9]+\s*>\s*)+(?:<[a-zA-Z0-9]+\b[^>]*>\s*)+)"
    r"([\u4e00-\u9fff]{2,4}(?:·[\u4e00-\u9fff]{2,4})?)(?=\s*<)",
    re.IGNORECASE,
)
_HTML_CROSS_ADDR_STRICT_RE = re.compile(
    r"((?:address|addr|receiver_?addr(?:ess)?|shipping_?addr(?:ess)?|consignee_?addr(?:ess)?|home_?addr(?:ess)?"
    r"|收货地址|配送地址|寄件地址|家庭住址|居住地址|详细住址|个人住址|户籍地址|联系地址|详细地址)"
    r"\s*[:：]?\s*(?:</[a-zA-Z0-9]+\s*>\s*)+(?:<[a-zA-Z0-9]+\b[^>]*>\s*)+)"
    r"([^<>]{4,80})(?=\s*<)",
    re.IGNORECASE,
)
_HTML_CROSS_ADDR_COOCCUR_RE = re.compile(
    r"((?:地址|住址|送至|配送至)"
    r"\s*[:：]?\s*(?:</[a-zA-Z0-9]+\s*>\s*)+(?:<[a-zA-Z0-9]+\b[^>]*>\s*)+)"
    r"([^<>]{4,80})(?=\s*<)",
    re.IGNORECASE,
)


def _has_personal_identity(text: str) -> bool:
    """文本是否含个人身份要素（手机号 / 身份证 / 显式姓名标签）。"""
    return bool(_PHONE_RE.search(text) or _IDCARD_RE.search(text) or _NAME_FIELD_RE.search(text))


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
    """返回 seg 内紧邻详细地址的行政区链尾结束位置（多段细节取最早），无则 -1。"""
    best_end = -1
    cur_end = -1
    for m in re.finditer(r"[\u4e00-\u9fff]{1,}?(省|自治区|市|区|县)", seg):
        word = m.group(0)
        if _NON_ADMIN_SUFFIX_RE.search(word):
            continue
        if cur_end == -1 or m.start() <= cur_end:
            cur_end = m.end()
        else:
            if best_end == -1 and _ADDR_DETAIL_RE.search(seg[cur_end : m.start()]):
                best_end = cur_end
            cur_end = m.end()
    return best_end if best_end != -1 else cur_end


def _mask_address_segment(seg: str, *, allow_street_only: bool = False) -> str:
    """疑似个人地址句：保留到最后一个行政区边界，其后整段掩为 ***。"""
    end = _last_admin_boundary(seg)
    if end <= 0 or end >= len(seg):
        return seg
    tail = seg[end:]
    if allow_street_only:
        if not _ADDR_DETAIL_RE.search(tail):
            return seg
    elif not _RESIDENTIAL_DETAIL_RE.search(tail):
        return seg
    return seg[:end] + "***"


def _mask_addresses(text: str, *, has_pii_context: bool = False) -> str:
    """逐句扫描个人地址：仅住宅户室粒度或与个人身份要素共现时掩到区县。"""
    parts = re.split(r"([^\n，。；;,.|<]+)", text)
    out: List[str] = []
    for seg in parts:
        if _ADDR_REGION_RE.search(seg) and _ADDR_DETAIL_RE.search(seg):
            seg = _ADDR_FIELD_RE.sub(lambda fm: fm.group(1) + _mask_address_value(fm.group(2)), seg)
            if has_pii_context:
                seg = _ADDR_COOCCUR_FIELD_RE.sub(lambda fm: fm.group(1) + _mask_address_value(fm.group(2)), seg)
            seg = _mask_address_segment(seg, allow_street_only=has_pii_context)
        out.append(seg)
    return "".join(out)


def _is_binary_data_uri(text: str) -> bool:
    """是否为 base64:// 或 data:*;base64, 二进制编码串（禁止跑正则以免破坏编码）。"""
    if text.startswith("base64://"):
        return True
    if text.startswith("data:") and ";base64," in text[:128]:
        return True
    return False


def mask_pii(text: str, *, has_pii_context: bool = False) -> str:
    """通用 PII 就地掩码。返回打码后的文本。"""
    if not text or _is_binary_data_uri(text):
        return text
    cooccur = has_pii_context or _has_personal_identity(text)
    out = _PHONE_RE.sub(lambda m: m.group(0)[:3] + "********", text)
    out = _IDCARD_RE.sub(lambda m: m.group(0)[:4] + "**********" + m.group(0)[-4:], out)
    out = _BANKCARD_RE.sub(lambda m: "****" + m.group(0)[-4:], out)
    out = _ADDR_FIELD_RE.sub(lambda m: m.group(1) + _mask_address_value(m.group(2)), out)
    if cooccur:
        out = _ADDR_COOCCUR_FIELD_RE.sub(
            lambda m: (
                m.group(1) + _mask_address_value(m.group(2))
                if (_ADDR_REGION_RE.search(m.group(2)) or _ADDR_DETAIL_RE.search(m.group(2)))
                else m.group(0)
            ),
            out,
        )
    out = _mask_addresses(out, has_pii_context=cooccur)
    out = _NAME_FIELD_RE.sub(lambda m: m.group(1) + m.group(2) + ("*" * len(m.group(3))), out)
    out = _BEARER_RE.sub("Bearer ****", out)
    out = _CRED_KV_RE.sub(lambda m: m.group(1) + "****", out)
    return out


def mask_pii_in_html(html: str) -> str:
    """HTML 渲染前脱敏：保护 style/script，支持跨标签键值与文本节点脱敏。"""
    if not html or _is_binary_data_uri(html):
        return html
    protected: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        idx = len(protected)
        protected.append(m.group(0))
        return f"\x00HTML_PROTECT_{idx}\x00"

    work = _HTML_PROTECT_RE.sub(_stash, html)
    visible_text = re.sub(r"<[^>]+>", " ", work)
    cooccur = _has_personal_identity(visible_text) or bool(_HTML_CROSS_NAME_RE.search(work))

    work = _HTML_CROSS_NAME_RE.sub(lambda m: m.group(1) + ("*" * len(m.group(2))), work)
    work = _HTML_CROSS_ADDR_STRICT_RE.sub(lambda m: m.group(1) + _mask_address_value(m.group(2).strip()), work)
    if cooccur:
        work = _HTML_CROSS_ADDR_COOCCUR_RE.sub(
            lambda m: (
                m.group(1) + _mask_address_value(m.group(2).strip())
                if (_ADDR_REGION_RE.search(m.group(2)) or _ADDR_DETAIL_RE.search(m.group(2)))
                else m.group(0)
            ),
            work,
        )
    # 首尾补哨兵括号：纯文本片段与首尾裸文本节点也走同一脱敏
    work = re.sub(
        r">([^<>]+)<",
        lambda m: ">" + mask_pii(m.group(1), has_pii_context=cooccur) + "<",
        ">" + work + "<",
    )[1:-1]
    for idx, block in enumerate(protected):
        work = work.replace(f"\x00HTML_PROTECT_{idx}\x00", block)
    return work


def pii_hits(text: str) -> list[str]:
    """返回命中的 PII 类型列表（审计用，不返回原文）。"""
    hits: list[str] = []
    if not text or _is_binary_data_uri(text):
        return hits
    cooccur = _has_personal_identity(text)
    if _PHONE_RE.search(text):
        hits.append("phone")
    if _IDCARD_RE.search(text):
        hits.append("id_card")
    if _BANKCARD_RE.search(text):
        hits.append("bank_card")
    if _ADDR_FIELD_RE.search(text):
        hits.append("address_field")
    elif _mask_addresses(text, has_pii_context=cooccur) != text or (
        cooccur
        and any(
            _ADDR_REGION_RE.search(m.group(2)) or _ADDR_DETAIL_RE.search(m.group(2))
            for m in _ADDR_COOCCUR_FIELD_RE.finditer(text)
        )
    ):
        hits.append("address")
    if _NAME_FIELD_RE.search(text):
        hits.append("name_field")
    if _BEARER_RE.search(text) or _CRED_KV_RE.search(text):
        hits.append("credential")
    return hits


def has_credential_form(text: str) -> bool:
    """文本是否含凭据形态（Bearer / token= / api_key= 等）。"""
    return bool(text) and not _is_binary_data_uri(text) and bool(_CRED_FORM_RE.search(text))


def mask_credential_forms(text: str) -> str:
    """只掩凭据形态（私聊输入闸用——保留手机号等私有记忆能力）。"""
    if not text or _is_binary_data_uri(text):
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
    if not text or _is_binary_data_uri(text):
        return text
    out = mask_credential_command(text)
    out = mask_credential_forms(out)
    return mask_pii(out) if is_group else out


# ── 上下文 parts 重建（输入闸） ─────────────────────────────────────


def apply_context_mask(parts: Sequence[_PartT], is_group: bool) -> List[_PartT]:
    """重建 ModelRequest.parts：所有文本字段过 mask_for_agent。

    - UserPromptPart.content（str 或 UserContent 列表）、ToolReturnPart/
      NativeToolReturnPart.content、TextPart.content、RetryPromptPart.content
      → 掩码副本
    - SystemPromptPart 与 ImageUrl / BinaryContent 等媒体对象跳过
    - 不原地改：parts 与 session.history 共享对象
    """
    from pydantic_core import ErrorDetails
    from pydantic_ai.messages import (
        TextPart,
        UserContent,
        ToolReturnPart,
        UserPromptPart,
        RetryPromptPart,
        SystemPromptPart,
        RetryFeedbackPart,
        NativeToolReturnPart,
    )

    masked: List[_PartT] = []
    for part in parts:
        if isinstance(part, SystemPromptPart):
            masked.append(part)
            continue
        if isinstance(part, UserPromptPart):
            content = part.content
            if isinstance(content, str):
                new_str = mask_for_agent(content, is_group=is_group)
                masked.append(part if new_str == content else replace(part, content=new_str))
            else:
                new_items: list[UserContent] = []
                changed = False
                for item in content:
                    if isinstance(item, str):
                        masked_item = mask_for_agent(item, is_group=is_group)
                        if masked_item != item:
                            changed = True
                        new_items.append(masked_item)
                    else:
                        new_items.append(item)
                masked.append(replace(part, content=new_items) if changed else part)
            continue
        if isinstance(part, TextPart):
            new_str = mask_for_agent(part.content, is_group=is_group)
            masked.append(part if new_str == part.content else replace(part, content=new_str))
            continue
        if isinstance(part, RetryPromptPart):
            if isinstance(part.content, str):
                new_str = mask_for_agent(part.content, is_group=is_group)
                masked.append(part if new_str == part.content else replace(part, content=new_str))
            else:
                masked.append(part)
            continue
        if isinstance(part, RetryFeedbackPart):
            if isinstance(part.content, str):
                new_str = mask_for_agent(part.content, is_group=is_group)
                masked.append(part if new_str == part.content else replace(part, content=new_str))
                continue
            changed_details = False
            new_details: list[ErrorDetails] = []
            for detail in part.content:
                new_msg = mask_for_agent(detail["msg"], is_group=is_group)
                detail_input = detail["input"]
                new_input = (
                    mask_for_agent(detail_input, is_group=is_group) if isinstance(detail_input, str) else detail_input
                )
                if new_msg == detail["msg"] and new_input == detail_input:
                    new_details.append(detail)
                    continue
                changed_details = True
                new_details.append({**detail, "msg": new_msg, "input": new_input})
            masked.append(replace(part, content=new_details) if changed_details else part)
            continue
        if isinstance(part, (ToolReturnPart, NativeToolReturnPart)):
            new_content = _mask_part_content(part.content, is_group)
            masked.append(part if new_content is part.content else replace(part, content=new_content))
            continue
        masked.append(part)
    return masked


def _mask_part_content(content: object, is_group: bool) -> object:
    """递归掩码工具返回中的文本叶子；媒体 dataclass 原样透传。"""
    if isinstance(content, str):
        return mask_for_agent(content, is_group=is_group)
    if isinstance(content, list):
        masked_list = [_mask_part_content(item, is_group) for item in content]
        return content if all(a is b for a, b in zip(masked_list, content)) else masked_list
    if isinstance(content, tuple):
        masked_tuple = tuple(_mask_part_content(item, is_group) for item in content)
        return content if all(a is b for a, b in zip(masked_tuple, content)) else masked_tuple
    if isinstance(content, dict):
        masked_dict = {k: _mask_part_content(v, is_group) for k, v in content.items()}
        return content if all(masked_dict[k] is content[k] for k in content) else masked_dict
    return content
