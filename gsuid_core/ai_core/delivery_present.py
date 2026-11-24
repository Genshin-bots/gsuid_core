"""出站呈现层制品交付与渲染（泄漏句柄补发、结构块分离、Markdown/render_agent 出图）。"""

from __future__ import annotations

import re
import json
import asyncio
import contextvars
from typing import (
    List,
    Tuple,
    Union,
    Mapping,
    Optional,
    Protocol,
)

from json_repair import repair_json

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.ai_core.control.lines import (
    CODE_SPAN_RE,
    MD_NUM_MARKER,
    MD_BULLET_MARKER,
)
from gsuid_core.ai_core.protocol_text import (
    _RESOURCE_HANDLE_RE,
    _RESOURCE_HANDLE_HINTS,
    _find_json_span,
    _strip_resource_handles,
)
from gsuid_core.utils.resource_manager import RM


# 同步读落盘图片：async 侧只能经 asyncio.to_thread 调它，文件不存在返回 None
def _read_image_artifact_bytes(payload_path: str) -> Optional[bytes]:
    from pathlib import Path

    p = Path(payload_path)
    if not p.exists():
        return None
    return p.read_bytes()


# markdown_dark.css 路径含 __file__ resolve（阻塞 IO），async 侧经 to_thread 取
def _markdown_dark_css_path() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "utils" / "html_render" / "markdown_dark.css")


class _LeakedHandleSender(Protocol):
    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        *,
        extra_metadata: Optional[Mapping[str, object]] = None,
    ) -> Optional[List[str]]: ...


async def _resolve_and_deliver_leaked_handles(
    text: str,
    bot: _LeakedHandleSender,
    extra_metadata: Optional[Mapping[str, object]] = None,
    ev: Event | None = None,
) -> str:
    """处理泄漏进正文的资源句柄：尽量把短指路对应的真实资源补发出去并抹掉句柄。"""
    if not any(h in text for h in _RESOURCE_HANDLE_HINTS):
        return text

    handles: list[str] = []
    for m in _RESOURCE_HANDLE_RE.finditer(text):
        h = m.group(0).strip("`")
        if h not in handles:
            handles.append(h)
    if not handles:
        return text

    stripped = re.sub(r"[ \t]{2,}", " ", _strip_resource_handles(text)).strip()

    # 正文本身已够长（≥120 字）→ 模型已把内容讲清楚，只是顺带提了句柄 → 不重复交付
    if bot is None or len(stripped) >= 120:
        return stripped

    inline_texts: list[str] = []
    for h in handles:
        try:
            if h.startswith("res_"):
                from gsuid_core.ai_core.outbound import (
                    lookup_res_owner,
                    speaker_may_take_res,
                    release_image_delivery,
                    note_run_image_delivered,
                    try_claim_image_delivery,
                )
                from gsuid_core.ai_core.planning.models import AIAgentArtifact
                from gsuid_core.ai_core.planning.kanban_executor import (
                    mark_root_delivered,
                    claim_inline_delivery,
                )

                art = await AIAgentArtifact.get_by_id(h)
                if art is None:
                    continue
                speaker = str(ev.user_id) if ev is not None and ev.user_id else ""
                looked = await lookup_res_owner(h)
                if looked.is_draft or looked.task_running:
                    continue
                if not speaker_may_take_res(looked, speaker):
                    logger.warning(
                        i18n_t(
                            "log.ai.delivery_owner_mismatch",
                            res_id=h,
                            owner=looked.owner_user_id,
                            target=speaker,
                        )
                    )
                    continue
                if art.payload_path and (art.mime or "").startswith("image/"):
                    # 先确认是这张图再占位，发送失败需释放避免 7 天内无法重发
                    image_data = await asyncio.to_thread(_read_image_artifact_bytes, art.payload_path)
                    if image_data is None:
                        continue
                    sid = str(ev.session_id) if ev is not None and ev.session_id else ""
                    claim = await try_claim_image_delivery(ev, h, session_id=sid)
                    if claim.refuse is not None:
                        continue
                    try:
                        await bot.send(MessageSegment.image(image_data), extra_metadata=extra_metadata)
                    except Exception as send_err:
                        logger.debug(i18n_t("log.ai.send_leaked_handle_could_not", h=h, e=send_err))
                        if claim.occupied:
                            await release_image_delivery(ev, h)
                        continue
                    if looked.root_task_id:
                        claim_inline_delivery(looked.root_task_id)
                        mark_root_delivered(looked.root_task_id)
                    note_run_image_delivered()
                    logger.info(i18n_t("log.ai.send_leaked_handle_was_resolved", h=h))
                elif art.payload_inline and art.payload_inline.strip():
                    inline_texts.append(art.payload_inline.strip())
            elif h.startswith("img_"):
                from gsuid_core.ai_core.outbound import note_run_image_delivered

                await bot.send(MessageSegment.image(await RM.get(h)), extra_metadata=extra_metadata)
                note_run_image_delivered()
                logger.info(i18n_t("log.ai.send_leaked_handle_was_resolved", h=h))
            # 非图片落盘文件与 aud_/vid_ 仅抹句柄不补发
        except Exception as e:
            logger.debug(i18n_t("log.ai.send_leaked_handle_could_not", h=h, e=e))

    if inline_texts:
        # 把文本 artifact 并进正文交后续管线（够长自动出图）
        joiner = "\n\n" if stripped else ""
        stripped = (stripped + joiner + "\n\n".join(inline_texts)).strip()
    return stripped


def _looks_like_tool_table(text: str) -> bool:
    """检测文本是否为工具结果 / 数据展示（含 markdown 表格或代码块）。"""
    if "```" in text:
        return True
    if re.search(r"\|.*\|.*\|", text) and ("---" in text or text.count("|") >= 4):
        return True
    return False


_HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def _normalize_html_linebreaks(text: str) -> str:
    """把模型吐出的 ``<br>`` 系列标签还原成真正的换行，代码块内保留原样。"""
    if not _HTML_BR_RE.search(text):
        return text

    parts: list[str] = []
    last = 0
    for m in CODE_SPAN_RE.finditer(text):
        parts.append(_HTML_BR_RE.sub("\n", text[last : m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_HTML_BR_RE.sub("\n", text[last:]))
    return "".join(parts)


def _strip_persona_markdown(text: str) -> str:
    """剥离闲聊/人格回复里的 markdown 与 ``*动作*`` 旁白；含表格/代码块时豁免。"""
    if _looks_like_tool_table(text):
        return text
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s{0,3}[-*>]\s+", "", text, flags=re.M)
    return text


_MD_HEADER_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_MD_HR_RE = re.compile(r"(?m)^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_MD_TABLE_ROW_RE = re.compile(r"\|.*\|.*\|")
_MD_BOLD_HEADER_RE = re.compile(r"(?m)^\s{0,3}\*\*[^*\n]{1,40}\*\*[：:]?\s*$")
_MD_NUM_LIST_RE = re.compile(rf"(?m)^\s{{0,3}}{MD_NUM_MARKER}\s+\S")
_MD_BULLET_RE = re.compile(rf"(?m)^\s{{0,3}}{MD_BULLET_MARKER}\s+\S")


def _has_markdown_table(text: str) -> bool:
    """是否含 markdown 表格（刻意不含代码块，保留代码文本可复制性）。"""
    if _MD_TABLE_ROW_RE.search(text) is None:
        return False
    return ("---" in text) or (text.count("|") >= 4)


_MARKDOWN_IMAGE_MIN_CHARS: int = 400
_MARKDOWN_IMAGE_MAX_WIDTH: int = 760


def _should_render_markdown_image(text: str) -> bool:
    """判断一段 AI 输出是否为结构化长 markdown 文档，值得整篇渲染成图下发。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("render_long_markdown_as_image").data:
        return False

    if len(text) < _MARKDOWN_IMAGE_MIN_CHARS:
        return False

    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) < 3:
        return False

    if _has_markdown_table(text):
        return True

    header_count = len(_MD_HEADER_RE.findall(text))
    if header_count >= 2:
        return True
    if header_count >= 1 and _MD_HR_RE.search(text) is not None:
        return True
    if len(_MD_BOLD_HEADER_RE.findall(text)) >= 2:
        return True
    if len(_MD_NUM_LIST_RE.findall(text)) >= 2:
        return True
    if len(_MD_BULLET_RE.findall(text)) >= 3:
        return True
    return False


_REPORT_BLOCK_RE = re.compile(
    r"<report(?:\s+title=(?:\"([^\"\n]*)\"|'([^'\n]*)'))?\s*>(.*?)</report\s*>",
    re.S | re.I,
)
_REPORT_TAG_ORPHAN_RE = re.compile(r"</?report(?:\s[^>\n]*)?>", re.I)


def _report_block_title(match: "re.Match[str]") -> str:
    return ((match.group(1) or match.group(2)) or "").strip()


_REPORT_FOOTER_TEMPLATE = "\n\n---\n\n> 本图由 Agent 自主生成 · ``render_md_to_bytes`` 渲染 · {ts}"


def _report_footer() -> str:
    import datetime

    return _REPORT_FOOTER_TEMPLATE.format(ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))


_FENCED_CODE_BLOCK_RE = re.compile(r"```(\w*)[ \t]*\r?\n?(.*?)```", re.S)
_OPEN_FENCE_RE = re.compile(r"```(\w*)[ \t]*\r?\n([\s\S]+)$")
_STRUCTURED_BLOCK_MIN_CHARS = 80
_TABLE_CHARS = frozenset("─│┌┐└┘├┤┬┴┼╔╗╚╝║═")
_DENSITY_THRESHOLD = 0.30


def _parse_json_dict(body: str) -> Optional[dict[object, object]]:
    """尝试把文本解析为 dict（含 repair）；失败返回 None。"""
    stripped = body.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        try:
            data = json.loads(repair_json(stripped))
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(data, dict):
        return data
    return None


def _is_structured_content(body: str, *, min_keys: int = 3) -> bool:
    """body 是否呈数据形态（可解析 JSON dict / 制表线 / 多行 key:value）。"""
    stripped = body.strip()
    data = _parse_json_dict(stripped)
    if data is not None and len(data) >= min_keys:
        return True
    if body.count("─") >= 3 or body.count("│") >= 3:
        return True
    kv_lines = [ln for ln in body.split("\n") if re.match(r"^\s*\S+[:：]\s*\S", ln)]
    if len(kv_lines) >= 4:
        return True
    return False


def _structural_density(paragraph: str) -> float:
    """段落结构化密度 0~1：JSON 字符 / 制表线 / kv 行 / 数字行占比，无域词表。"""
    if not paragraph or len(paragraph) < 40:
        return 0.0
    data = _parse_json_dict(paragraph)
    if data is not None and len(data) >= 2:
        return 1.0
    lines = paragraph.split("\n")
    total_chars = len(paragraph)
    score = 0.0
    json_chars = sum(1 for c in paragraph if c in '{}":,')
    if total_chars > 0:
        json_ratio = json_chars / total_chars
        if json_ratio > 0.08:
            score += min(json_ratio * 3, 0.4)
    table_chars = sum(1 for c in paragraph if c in _TABLE_CHARS)
    if total_chars > 0:
        table_ratio = table_chars / total_chars
        if table_ratio > 0.03:
            score += min(table_ratio * 5, 0.4)
    kv_lines = sum(1 for ln in lines if re.match(r"^\s*\S+[:：]\s*\S", ln))
    if len(lines) >= 3:
        kv_ratio = kv_lines / len(lines)
        if kv_ratio > 0.5:
            score += min(kv_ratio * 0.4, 0.3)
    if len(lines) >= 5:
        digit_lines = sum(1 for ln in lines if re.search(r"\d+\.?\d*", ln))
        if digit_lines / len(lines) > 0.5:
            score += 0.15
    return score


def _infer_block_title(body: str) -> str:
    """从数据形态推断标题：优先短字符串值，否则首行，否则默认。"""
    stripped = body.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```\w*\s*", "", stripped).strip()
    data = _parse_json_dict(stripped)
    if data is not None:
        for v in data.values():
            if isinstance(v, str) and 0 < len(v.strip()) <= 40:
                return v.strip()
        for k in data:
            if isinstance(k, str) and k:
                return k[:40]
    first_line = stripped.split("\n")[0].strip()
    if first_line and first_line not in "{[" and len(first_line) <= 40:
        return first_line
    return "分析资料"


def _fence_body_is_data(body: str) -> bool:
    """围栏内 body 是否应进制品通道。"""
    if not body.strip():
        return False
    if _is_structured_content(body, min_keys=2):
        return True
    if len(body) >= _STRUCTURED_BLOCK_MIN_CHARS and _structural_density(body) >= _DENSITY_THRESHOLD:
        return True
    return False


def _extract_embedded_structured_blocks(
    text: str,
) -> Tuple[str, List[Tuple[str, str]]]:
    """按内容形态抽出数据块，与包装名无关（```report / 裸 JSON / 制表线均可）。"""
    reports: List[Tuple[str, str]] = []

    def _try_extract_fence(match: "re.Match[str]") -> str:
        body = match.group(2).strip()
        if not _fence_body_is_data(body):
            return match.group(0)
        reports.append((_infer_block_title(body), body))
        return ""

    remaining = _FENCED_CODE_BLOCK_RE.sub(_try_extract_fence, text)

    def _open_fence_data_and_tail(body: str) -> Optional[Tuple[str, str]]:
        """未闭合 fence：只切数据前缀，尾部台词回 speech。无数据形态则 None。"""
        stripped = body.strip()
        if not stripped:
            return None
        span = _find_json_span(stripped)
        if span is not None and _fence_body_is_data(span):
            idx = body.find(span)
            if idx < 0:
                idx = 0
            tail = body[idx + len(span) :]
            return span, tail
        # 空白分段：首段是表/kv 数据、尾段低密度台词
        parts = re.split(r"\n\s*\n", body, maxsplit=1)
        if len(parts) == 2 and _fence_body_is_data(parts[0]) and _structural_density(parts[1]) < 0.25:
            return parts[0].strip(), parts[1]
        if _fence_body_is_data(body):
            return stripped, ""
        return None

    def _try_open_fence(match: "re.Match[str]") -> str:
        body = match.group(2)
        # 体内还有闭合围栏已由 _FENCED_CODE_BLOCK_RE 处理，勿当截断 fence 再切
        if "```" in body:
            return match.group(0)
        split = _open_fence_data_and_tail(body)
        if split is None:
            return match.group(0)
        data_part, tail = split
        reports.append((_infer_block_title(data_part), data_part))
        return tail

    remaining = _OPEN_FENCE_RE.sub(_try_open_fence, remaining)

    paragraphs = re.split(r"(\n\s*\n)", remaining)
    kept_parts: List[str] = []
    for part in paragraphs:
        stripped = part.strip()
        if not stripped or part.isspace():
            kept_parts.append(part)
            continue
        density = _structural_density(stripped)
        json_hit = _is_structured_content(stripped, min_keys=3) and stripped.startswith("{")
        if (
            density >= 0.5
            or (density >= _DENSITY_THRESHOLD and len(stripped) >= _STRUCTURED_BLOCK_MIN_CHARS)
            or (json_hit and len(stripped) >= _STRUCTURED_BLOCK_MIN_CHARS)
        ):
            reports.append((_infer_block_title(stripped), stripped))
        else:
            kept_parts.append(part)

    remaining = "".join(kept_parts)

    span = _find_json_span(remaining)
    if span is not None and len(span) >= _STRUCTURED_BLOCK_MIN_CHARS:
        data = _parse_json_dict(span)
        if data is not None and len(data) >= 3:
            reports.append((_infer_block_title(span), span))
            remaining = remaining.replace(span, "").strip()

    return remaining.strip(), reports


def _extract_legacy_report_blocks(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """遗留：剥 ``<report>`` 标签，body 进制品图通道（协议已废止，仅兼容漏网）。"""
    reports: List[Tuple[str, str]] = []

    def _collect(match: "re.Match[str]") -> str:
        title = _report_block_title(match)
        body = match.group(3).strip()
        if body:
            reports.append((title, body))
        return ""

    remaining = _REPORT_BLOCK_RE.sub(_collect, text)
    remaining = _REPORT_TAG_ORPHAN_RE.sub("", remaining)
    return remaining.strip(), reports


_extract_report_blocks = _extract_legacy_report_blocks


def _split_speech_and_artifacts(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """发送/入史共用的两通道分离：内容形态结构化块 + 遗留 XML report 兼容。"""
    speech, xml_blocks = _extract_legacy_report_blocks(text)
    speech, embedded = _extract_embedded_structured_blocks(speech)
    return speech, embedded + xml_blocks


_PRESENT_RENDER_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("present_render_depth", default=0)


def _present_render_task(md: str) -> str:
    return (
        "把下面这份已经写好的正文排成一张手机竖屏信息卡。\n"
        "事实只许来自正文：禁止改数字、专名、剂量、结论和警示句，禁止补写正文里没有的内容。\n"
        "禁止调用 render_markdown_to_image。只用 gs-page 写一份 HTML，只调用一次 render_html_to_image。\n"
        "出图后的返回值必须带上图片 res_ 句柄，摘要最多一句。\n\n"
        f"正文：\n{md}"
    )


def _persona_allows_render_agent(ev: Event | None) -> bool:
    if ev is None:
        return False
    from gsuid_core.ai_core.persona.settings import persona_name_from_event
    from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

    return persona_allows_capability_agent(persona_name_from_event(ev), "render_agent")


async def _send_registered_image(
    handle: str,
    bot: Bot,
    ev: Event,
    extra_metadata: Optional[Mapping[str, object]],
) -> bool:
    """把本轮 render_agent 刚登记的图片发出去。临时任务没有发起人记录，不走跨用户归属校验。"""
    from gsuid_core.ai_core.outbound import (
        release_image_delivery,
        note_run_image_delivered,
        try_claim_image_delivery,
    )
    from gsuid_core.ai_core.planning.models import AIAgentArtifact

    art = await AIAgentArtifact.get_by_id(handle)
    if art is None or not art.payload_path or not art.mime.startswith("image/"):
        return False
    image_data = await asyncio.to_thread(_read_image_artifact_bytes, art.payload_path)
    if image_data is None:
        return False
    sid = str(ev.session_id) if ev.session_id else ""
    claim = await try_claim_image_delivery(ev, handle, session_id=sid)
    if claim.refuse is not None:
        return True
    try:
        await bot.send(MessageSegment.image(image_data), extra_metadata=extra_metadata)
    except Exception as send_err:
        logger.warning(i18n_t("log.ai.send_chat_result_render_agent_fail", e=send_err))
        if claim.occupied:
            await release_image_delivery(ev, handle)
        return False
    note_run_image_delivered()
    logger.info(i18n_t("log.ai.send_chat_result_render_agent_image", p0=len(image_data)))
    return True


async def _try_present_via_render_agent(
    md: str,
    bot: Bot,
    ev: Event | None,
    extra_metadata: Optional[Mapping[str, object]] = None,
) -> bool:
    """长正文先交给 render_agent。成功发出图片返回 True。"""
    if ev is None or _PRESENT_RENDER_DEPTH.get() > 0 or not _persona_allows_render_agent(ev):
        return False
    import time
    import hashlib

    from gsuid_core.ai_core.mcp.pii import mask_pii
    from gsuid_core.ai_core.capability_agents.runner import (
        CAPABILITY_AGENT_ERROR_PREFIX,
        run_capability_agent,
    )

    if ev.group_id:
        md = mask_pii(md)
    token = _PRESENT_RENDER_DEPTH.set(_PRESENT_RENDER_DEPTH.get() + 1)
    raw = ""
    try:
        suffix = hashlib.md5(f"{time.time_ns()}:{len(md)}".encode()).hexdigest()[:10]
        raw = await run_capability_agent(
            profile_id="render_agent",
            task=_present_render_task(md),
            ev=ev,
            bot=bot,
            session_id_suffix=f"present_{suffix}",
        )
    except Exception as e:
        logger.warning(i18n_t("log.ai.send_chat_result_render_agent_fail", e=e))
        raw = ""
    finally:
        _PRESENT_RENDER_DEPTH.reset(token)
    if not raw:
        return False
    if raw.startswith(CAPABILITY_AGENT_ERROR_PREFIX):
        logger.warning(i18n_t("log.ai.send_chat_result_render_agent_fail", e=raw[:180]))
        return False
    sent = False
    seen: list[str] = []
    for match in _RESOURCE_HANDLE_RE.finditer(raw):
        handle = match.group(0).strip("`")
        if not handle.startswith("res_") or handle in seen:
            continue
        seen.append(handle)
        if await _send_registered_image(handle, bot, ev, extra_metadata):
            sent = True
    if not sent:
        logger.info(i18n_t("log.ai.send_chat_result_render_agent_miss"))
    return sent


async def _send_report_images(
    reports: List[Tuple[str, str]],
    bot: Bot,
    extra_metadata: Optional[Mapping[str, object]] = None,
    ev: Event | None = None,
) -> None:
    """制品通道出图：结构化块 / 遗留 report body → 信息卡，失败再退 markdown 图。"""
    from gsuid_core.ai_core.mcp.pii import mask_pii
    from gsuid_core.utils.html_render import render_md_to_bytes

    is_group = bool((ev is not None and ev.group_id) or (bot.ev is not None and bot.ev.group_id))
    css_path = await asyncio.to_thread(_markdown_dark_css_path)
    for title, body in reports:
        md = f"# {title}\n\n{body}" if title else body
        if is_group:
            md = mask_pii(md)
        if await _try_present_via_render_agent(md, bot, ev, extra_metadata):
            continue
        md = f"{md}{_report_footer()}"
        try:
            image_bytes = await render_md_to_bytes(
                md=md,
                css_path=css_path,
                max_width=_MARKDOWN_IMAGE_MAX_WIDTH,
                image_format="png",
                dark=False,
            )
        except Exception as e:
            logger.warning(i18n_t("log.ai.send_chat_result_render_report_artifact_fail", e=e))
            await bot.send(MessageSegment.text(md), extra_metadata=extra_metadata)
            continue
        await bot.send(MessageSegment.image(image_bytes), extra_metadata=extra_metadata)
        logger.info(i18n_t("log.ai.send_chat_result_report_artifact", p0=len(image_bytes)))


async def _try_render_markdown_image(
    md: str,
    bot: Bot,
    extra_metadata: Optional[Mapping[str, object]] = None,
) -> bool:
    """把整篇 markdown 渲染成一张图片下发；失败返回 False 以退回拆条文本。"""
    from gsuid_core.ai_core.mcp.pii import mask_pii
    from gsuid_core.utils.html_render import render_md_to_bytes

    if bot.ev is not None and bot.ev.group_id:
        md = mask_pii(md)
    try:
        css_path = await asyncio.to_thread(_markdown_dark_css_path)
        image_bytes = await render_md_to_bytes(
            md=f"{md}{_report_footer()}",
            css_path=css_path,
            max_width=_MARKDOWN_IMAGE_MAX_WIDTH,
            image_format="png",
            dark=False,
        )
    except Exception as e:
        logger.warning(i18n_t("log.ai.send_chat_result_render_long_markdown_fail", e=e))
        return False

    await bot.send(MessageSegment.image(image_bytes), extra_metadata=extra_metadata)
    logger.info(i18n_t("log.ai.send_chat_result_long_markdown_rendered", p0=len(image_bytes)))
    return True
