"""出站呈现与发送（I/O 层：拆条、制品出图、泄漏句柄补发、表情包与 @ 解析）。"""

from __future__ import annotations

import re
import json
import asyncio
import contextvars
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Tuple,
    Union,
    Mapping,
    Callable,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

from PIL import Image
from json_repair import repair_json

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.ai_core.agent_errors import strip_framework_user_leaks
from gsuid_core.ai_core.control.lines import (
    CODE_SPAN_RE,
    MD_NUM_MARKER,
    MD_BULLET_MARKER,
)
from gsuid_core.ai_core.protocol_text import (
    _RESOURCE_HANDLE_RE,
    _RESOURCE_HANDLE_HINTS,
    _find_json_span,
    is_silence_marker,
    _strip_resource_handles,
    _strip_tool_call_artifacts,
    _strip_special_control_tokens,
    remainder_after_protocol_tags,
)
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.angle_bracket_guard import MEME_TAG_PATTERN

if TYPE_CHECKING:
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.reply_plan import ReplyGroup

# 表情没有字数，按 7 个字计，间隔是 1 秒。
MEME_PAUSE_CHARS = 7


def bubble_pause(char_count: int) -> float:
    """两条出站消息的间隔。字数 / 7 秒，最短 0.5，最长 3。"""
    return min(max(char_count / 7, 0.5), 3.0)


async def pause_before_meme() -> None:
    """表情包发出前等待，和文字气泡用同一条间隔。"""
    await asyncio.sleep(bubble_pause(MEME_PAUSE_CHARS))


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
    """处理泄漏进正文的资源句柄：尽量把它**指向的真实资源交付出去**，而不是简单删掉句柄
    留下"详细的放那里面了 …… 自己看吧"这种指向空气的破碎引用——那样对用户反而更莫名其妙
    （尤其图片句柄：模型压根没发图，删了 ID 用户就啥也没有）。

    仅当正文除句柄外**基本只剩一句短指路**（模型偷懒只甩了个句柄、没真正把内容讲出来）时才补发：
      - 图片 artifact（``res_`` 落盘 image/* 或 ``img_`` RM）→ 直接把图发出去，剩下的短句成为自然图注；
      - 纯文本 artifact（``res_`` inline）→ 把内容并进正文，走后续管线（够长会自动出图），让"自己看"有实物。
    若正文本身已经够长（模型其实已把结论讲清楚、只是顺带提了句柄）→ 只抹句柄、不重复交付。
    解析失败 / 拿不到资源 / 非图片文件 → 退回"只抹句柄"。全程 try/except，绝不因补发失败阻断发送。

    **无论如何都保证句柄被抹除**（返回值里不含任何 res_/img_ 句柄），补发只是"尽力而为"的增强。
    """
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
                from gsuid_core.ai_core.planning.kanban_executor import claim_inline_delivery

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


# 病理级硬顶；主人格长文控长走 FileOS，勿在此用 4k 砍刀污染落盘真身
_TOOL_RESULT_SAFETY_MAX_CHARS = 2_000_000


@runtime_checkable
class _ModelDumpJson(Protocol):
    def model_dump_json(self) -> str: ...


@runtime_checkable
class _JsonDumpable(Protocol):
    def json(self) -> str: ...


async def handle_tool_result(
    bot: Optional[Bot],
    result: object,
    max_length: int = _TOOL_RESULT_SAFETY_MAX_CHARS,
) -> str:
    """序列化工具返回；Message 会尝试 send。默认仅病理级长度硬顶。"""
    if isinstance(result, Message):
        a = "生成内容成功!"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关消息！"
        else:
            a += ", 由于没有Bot对象, 未发送相关消息！"
        return a
    elif isinstance(result, str):
        res_str = result
    elif isinstance(result, dict):
        res_str = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, Image.Image):
        img_bytes = await convert_img(result)
        a = f"生成了图片资源, 资源ID: {RM.register(img_bytes)}"
        if bot is not None:
            await bot.send(img_bytes)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, bytes):
        a = f"生成了某项资源, 资源ID: {RM.register(result)}"
        if bot is not None:
            await bot.send(result)
            a += ", 已经发送了相关资源！"
        else:
            a += ", 由于没有Bot对象, 未发送相关资源！"
        return a
    elif isinstance(result, list):
        res_str = json.dumps(result, ensure_ascii=False)
    elif isinstance(result, _ModelDumpJson):
        res_str = result.model_dump_json()
    elif isinstance(result, _JsonDumpable):
        res_str = result.json()
    else:
        res_str = str(result)

    # 【读窗口】分页体永不砍，避免 offset 续读丢页
    if len(res_str) > max_length:
        if "【读窗口】" in res_str[:800] or "…[分页 " in res_str or "\n[分页 " in res_str:
            return res_str
        return res_str[:max_length] + f"\n...[系统截断: 省略后 {len(res_str) - max_length} 字符]"
    return res_str


def _looks_like_tool_table(text: str) -> bool:
    """检测文本是否为工具结果 / 数据展示（含 markdown 表格或代码块）。

    用于在 ``send_chat_result`` 中豁免 markdown 净化——闲聊回复剥离 markdown，
    但工具检索结果（如表格）需保留原样。
    """
    if "```" in text:
        return True
    if re.search(r"\|.*\|.*\|", text) and ("---" in text or text.count("|") >= 4):
        return True
    return False


_HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def _normalize_html_linebreaks(text: str) -> str:
    """把模型吐出的 ``<br>`` 系列标签还原成真正的换行。

    模型会用 ``<br>`` 代替换行——框架自己的 prompt 里就大量使用尖括号标记
    （``<example>`` / ``<meme: 困>`` / ``<SILENCE>``），模型被这种"这里可以打标记"的
    语境带偏。IM 不渲染 HTML，用户看到的是字面的 ``xxx<br><br>xxx``。

    更要命的是 `send_chat_result` 靠 ``\\n\\s*\\n`` **拆分多条消息**：``<br>`` 会让这个
    拆分**完全失效**，人格卡里"连发 2-3 条短消息"退化成一整段带标签的怪文本。
    还原成换行后，模型的原意（换行 / 连发多条）自然恢复。
    """
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
    """剥离闲聊/人格回复里的 markdown 与 ``*动作*`` 旁白（B-2）。

    QQ 等 IM 不渲染 markdown，字面的 ``**加粗**`` / ``*揉揉眼睛*`` 极不拟人。
    但**工具结果**（含表格 / 代码块）需保留原样以便阅读，故命中
    ``_looks_like_tool_table`` 时原样返回、不做任何剥离。
    """
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
    """是否含 markdown 表格。

    **刻意不含代码块**（与 ``_looks_like_tool_table`` 的区别）：代码块用户往往要**复制**，
    出成图片反而不能选中，故长代码答复保留原有"文本"行为，不纳入整篇出图。
    """
    if _MD_TABLE_ROW_RE.search(text) is None:
        return False
    return ("---" in text) or (text.count("|") >= 4)


_MARKDOWN_IMAGE_MIN_CHARS: int = 400
_MARKDOWN_IMAGE_MAX_WIDTH: int = 760
_INBOX_MAX_TARGETS: int = 3
_INBOX_MAX_BUBBLES: int = 3


def _should_render_markdown_image(text: str) -> bool:
    """判断一段 AI 输出是否是"结构化长 markdown 文档"，值得整篇渲染成一张图片下发。

    动机：``send_chat_result`` 默认按空行（``\\n\\n``）把文本拆成多条消息逐条下发——这本是
    人格"连发 2-3 条短消息"的能力，但 agent 产出的长报告（多标题 + 表格 + 分隔线）
    会因此被拆成几十条刷屏，且 IM 不渲染 markdown，用户看到的是满屏字面 ``**`` / ``|``。

    判定**刻意保守**，只在"确实是文档"时命中，绝不误伤日常连发短句：
      - 必须够长（≥ ``_MARKDOWN_IMAGE_MIN_CHARS``）；
      - 且拆分后 ≥3 段（单段短文没有出图必要）；
      - 且含明确结构信号：**表格** / ≥2 个 ATX 标题 / ≥2 个整行粗体小标题 /
        编号列表≥2 项 / 无序列表≥3 项 /（水平分割线 且 ≥1 个标题）。
    仅靠"多个空行段落"绝不命中——纯口语连发短句没有表格 / 标题 / 列表。
    （agent 长报告常用 ``**粗体小标题** + 编号建议`` 而非 markdown 表格/# 标题，故一并纳入。）
    代码块**不**触发出图：用户往往要复制代码，保留文本行为（见 ``_has_markdown_table``）。
    """
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
_ERROR_OUTPUT_RE = re.compile(r"执行出错[:：]|status_code:\s*\d{3}|Traceback|rate_limit|APIError|TimeoutError")


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


def _parse_json_dict(body: str) -> Optional[dict]:
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
    """围栏内 body 是否应进制品通道。

    围栏是位置先验（模型把数据装进 fence 时 lang 名不可信），阈值比裸文本更松：
    JSON ≥2 键即可；或密度/制表线达标。
    """
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


def _split_speech_and_artifacts(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    """发送/入史共用的两通道分离：内容形态结构化块 + 遗留 XML report 兼容。

    主契约已是委派 ``render_agent`` 出图；此处只做呈现层兜底，
    避免模型把表格/JSON/旧 ``<report>`` 当台词刷屏。
    """
    speech, xml_blocks = _extract_legacy_report_blocks(text)
    speech, embedded = _extract_embedded_structured_blocks(speech)
    return speech, embedded + xml_blocks


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
    """制品通道出图：结构化块 / 遗留 report body → 信息卡，失败再退 markdown 图。

    主路径应已由 ``render_agent`` 出图；此处是呈现层兜底。
    """
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
    """把整篇 markdown 渲染成一张图片下发。

    成功返回 ``True``；渲染失败返回 ``False``，由调用方**优雅降级**回按空行拆条的原逻辑
    （宁可刷屏也不要丢消息）。
    """
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


_MENTION_LABEL_RE = re.compile(r"([^\s()（）\n]{1,16})\(用户ID:([^)]+)\)")


def mention_names_in_text(text: str) -> Dict[str, str]:
    """从「名(用户ID:id)」抽出可解析的提及。"""
    names: Dict[str, str] = {}
    for match in _MENTION_LABEL_RE.finditer(text or ""):
        name = match.group(1).strip()
        uid = match.group(2).strip()
        if name and uid and name != uid:
            names[name] = uid
    return names


def mention_names_from_event(ev: Event | None) -> Dict[str, str]:
    if ev is None:
        return {}
    sender = ev.sender
    nick = ""
    if isinstance(sender, dict) and "nickname" in sender and isinstance(sender["nickname"], str):
        nick = sender["nickname"].strip()
    uid = str(ev.user_id).strip() if ev.user_id else ""
    if nick and uid and nick != uid:
        return {nick: uid}
    return {}


def rewrite_nickname_mentions(
    text: str,
    names: Dict[str, str],
    *,
    already_at: str | None = None,
) -> str:
    """<@昵称> 收成 <@用户ID>。已经在提醒的人去掉重复；对不上的括号删掉。裸 @ 保留。"""
    from gsuid_core.ai_core.outbound import sub_mentions, is_mention_id, normalize_mention_token

    def _repl(match: re.Match[str]) -> str:
        token = normalize_mention_token(match.group(1))
        if not token:
            return ""
        uid = names[token] if token in names else ""
        if uid:
            if already_at and uid == already_at:
                return ""
            return f"<@{uid}>"
        if is_mention_id(token):
            return f"<@{token}>"
        return ""

    return sub_mentions(text, _repl)


def _persona_max_bubbles(ev: Event | None) -> int:
    """单轮主通道气泡上限。按当前人格的 ``chat_style`` 派生，缺人格回落默认档。"""
    from gsuid_core.ai_core.persona.settings import persona_name_from_event
    from gsuid_core.ai_core.persona.chat_style import resolve_chat_style

    return resolve_chat_style(persona_name_from_event(ev)).bubbles


def _outbound_auditor(ev: Event | None) -> Optional[Callable[[str, int, int], None]]:
    """主通道出站审计回调。拿不到 session 时返回 None（评估 / 子代理等无归属场景）。

    session 在发送前解析一次，不在气泡循环里反复查注册表。
    """
    if ev is None or not ev.session_id:
        return None
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    sess = get_ai_session_registry().get_ai_session(ev.session_id)
    if sess is None or sess._session_logger is None:
        return None

    group_id = str(ev.group_id) if ev.group_id else ""
    target_user = str(ev.user_id) if ev.user_id else ""
    session_logger = sess._session_logger

    def _log(text: str, index: int, total: int) -> None:
        session_logger.log_outbound_audit(
            group_id=group_id,
            text=text,
            image_id="",
            topic="",
            target_user=target_user,
            bubble_index=index,
            bubble_total=total,
        )

    return _log


def pack_outbound_bubbles(blocks: Sequence[str], budget: int) -> list[str]:
    """按气泡预算无损打包分段文本；超出预算的尾部合并进最后一个气泡。"""
    cleaned = [b.strip() for b in blocks if b.strip()]
    if not cleaned or budget <= 0:
        return []
    if len(cleaned) <= budget:
        return cleaned
    head = cleaned[: budget - 1]
    tail = "\n\n".join(cleaned[budget - 1 :])
    return [*head, tail]


async def send_chat_result(
    bot: Bot,
    text: str,
    ev: Event | None = None,
    extra_metadata: Optional[Mapping[str, object]] = None,
    ooc_check: bool = True,
    at_user_id: str | None = None,
    mention_names: Optional[Dict[str, str]] = None,
    _reply_prefix: Optional[Sequence[Message]] = None,
    _plan_group: bool = False,
    _reply_target: Optional[Tuple[int, str]] = None,
) -> None:
    """
    解析并发送聊天结果，支持：
    - 按换行分割多条消息
    - <@用户ID> 或 <@昵称> → MessageSegment.at。裸 @ 不解析
    - at_user_id：框架强制前置 @（任务交付回灌等，不依赖模型自觉）
    - <meme: 情绪> 标记（可带反引号；尖括号和冒号旁边的空格也认）→ 触发表情包发送（需传入 ev）
    - extra_metadata：透传到 ``Bot.send`` 的 ``extra_metadata``，最终落到
      ``message_history`` 记录上（如主动消息的 ``proactive=True / source / reason``）
    - ooc_check：出戏防火墙开关。gs_agent 自判产物已走过系统提醒，传 False 放行
    """
    if not text:
        return

    meta: dict[str, object] = dict(extra_metadata) if extra_metadata else {}
    if "speech_channel" not in meta:
        meta["speech_channel"] = "persona"
    extra_metadata = meta

    _trimmed = text.strip()
    if is_silence_marker(_trimmed):
        logger.debug(i18n_t("log.ai.send_chat_result_special_trimmed_skip", _trimmed=repr(_trimmed)))
        return

    # 必须在 remainder_after_protocol_tags 剥 <reply #n> 之前解析。
    _turn = None
    if meta["speech_channel"] == "persona" and not _plan_group:
        from gsuid_core.ai_core.inbox.turn import current_turn

        _turn = current_turn()
    if _turn is not None:
        from gsuid_core.ai_core.reply_plan import parse_reply_plan

        _valid = {e.item.seq for e in _turn.entries}
        _plan = parse_reply_plan(
            _trimmed,
            valid_seqs=frozenset(_valid),
            default_seqs=(_turn.primary.item.seq,),
            max_targets=_INBOX_MAX_TARGETS,
        )
        if _plan.invalid_refs:
            logger.info(i18n_t("log.inbox.reply_ref_invalid", seqs=",".join(str(s) for s in _plan.invalid_refs)))
        if _plan.silence:
            return
        if _plan.groups:
            await _send_reply_plan_groups(
                bot,
                _plan.groups,
                turn=_turn,
                ev=ev,
                meta=meta,
                ooc_check=ooc_check,
            )
            return

    _speech = remainder_after_protocol_tags(_trimmed).strip()
    if not _speech:
        return
    _speech = strip_framework_user_leaks(_speech)
    if not _speech:
        return
    text = _speech

    if _ERROR_OUTPUT_RE.search(text):
        logger.warning(i18n_t("log.ai.send_chat_result_intercepted_fail", text=text[:100]))
        text = "脑子转不动了，等下再说。"

    text = _strip_tool_call_artifacts(text)
    text = _strip_special_control_tokens(text)
    text = await _resolve_and_deliver_leaked_handles(text, bot, extra_metadata, ev=ev)
    text, report_blocks = _split_speech_and_artifacts(text)

    from gsuid_core.ai_core.angle_bracket_guard import (
        find_illegal_angle_tags,
        sanitize_illegal_angle_tags,
    )

    _ab_leaks = find_illegal_angle_tags(text)
    if _ab_leaks:
        logger.warning(
            i18n_t(
                "log.ai.send_chat_result_sanitize_residual_tags",
                tags=_ab_leaks[:4],
                preview=repr(text[:80]),
            )
        )
        text = sanitize_illegal_angle_tags(text)
        if not text.strip() and not report_blocks:
            return

    text = _normalize_html_linebreaks(text)
    from gsuid_core.ai_core.outbound import rewrite_cq_at, strip_mention_tags

    text = rewrite_cq_at(text)
    _names = mention_names_from_event(ev)
    if mention_names:
        _names.update(mention_names)
    text = rewrite_nickname_mentions(text, _names, already_at=at_user_id)

    logger.trace(i18n_t("log.ai.meme_text_raw_output", text=repr(text)))

    meme_tags: list[str] = MEME_TAG_PATTERN.findall(text)
    md_source: str = MEME_TAG_PATTERN.sub("", text).strip()

    clean_text: str = _strip_persona_markdown(md_source)
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"^[，。！？\s]+|[，。！？\s]+$", "", clean_text)

    _ooc_replaced = False
    if (clean_text or report_blocks) and ooc_check:
        from gsuid_core.ai_core.control.outbound_detectors import (
            inspect_outbound_ooc,
            is_ooc_firewall_enabled,
        )

        if is_ooc_firewall_enabled():
            _user_text = ev.raw_text if ev is not None and ev.raw_text else ""
            _hit = inspect_outbound_ooc(clean_text, user_text=_user_text) if clean_text else None
            if _hit is not None:
                logger.warning(
                    i18n_t(
                        "log.ai.firewall_result_hit_ooc_red",
                        p0=_hit.category,
                        p1=_hit.matched,
                    )
                )
                clean_text = ""
                _ooc_replaced = True
            if report_blocks:
                _kept_blocks: List[Tuple[str, str]] = []
                for _r_title, _r_body in report_blocks:
                    _r_hit = inspect_outbound_ooc(_r_body, user_text=_user_text)
                    if _r_hit is None:
                        _kept_blocks.append((_r_title, _r_body))
                    else:
                        logger.warning(
                            i18n_t(
                                "log.ai.firewall_report_artifact_block",
                                p0=_r_hit.category,
                                p1=_r_hit.matched,
                            )
                        )
                report_blocks = _kept_blocks

    async def _send_trailing_artifacts() -> None:
        if report_blocks:
            await _send_report_images(report_blocks, bot, extra_metadata, ev)
        if meme_tags and ev is not None:
            await _send_meme_from_tag(meme_tags[0].strip(), bot, ev)

    logger.trace(i18n_t("log.ai.meme_parsed_tags_cleaned", meme_tags=meme_tags, clean_text=repr(clean_text)))

    if not clean_text:
        await _send_trailing_artifacts()
        return

    if not _ooc_replaced and _should_render_markdown_image(md_source):
        if await _try_present_via_render_agent(md_source, bot, ev, extra_metadata):
            await _send_trailing_artifacts()
            return
        if await _try_render_markdown_image(md_source, bot, extra_metadata):
            await _send_trailing_artifacts()
            return

    _PERSONA_MAX_BUBBLES = _persona_max_bubbles(ev)
    raw_blocks = [b for b in re.split(r"\n\s*\n", clean_text) if b.strip()]
    _plan_turn = None
    if meta["speech_channel"] == "persona":
        from gsuid_core.ai_core.inbox.turn import current_turn

        _plan_turn = current_turn()

    _budget = _PERSONA_MAX_BUBBLES
    if _plan_turn is not None:
        _turn_rem = max(0, _INBOX_MAX_BUBBLES - _plan_turn.bubbles_sent)
        _budget = min(_PERSONA_MAX_BUBBLES, _turn_rem)

    if _budget <= 0:
        if raw_blocks:
            logger.info(i18n_t("log.inbox.bubble_capped", count=len(raw_blocks)))
        await _send_trailing_artifacts()
        return

    if len(raw_blocks) > _budget:
        logger.debug(i18n_t("log.ai.persona_bubbles_clamped", p0=_budget))
    blocks = pack_outbound_bubbles(raw_blocks, _budget)
    _force_at = (at_user_id or "").strip()
    _at_done = False
    _audit = _outbound_auditor(ev)
    _total = len(blocks)
    _prefix = list(_reply_prefix) if _reply_prefix else []
    _deferred_reply = _reply_target

    for _idx, block in enumerate(blocks, 1):
        if not block.strip():
            continue

        segments = _parse_at_segments(block)
        if _prefix:
            segments = [*_prefix, *segments]
            _prefix = []
        _has_force_in_block = any(s.type == "at" and str(s.data or "") == _force_at for s in segments)
        if _force_at and not _at_done:
            if not _has_force_in_block:
                segments = [MessageSegment.at(_force_at), *segments]
            _at_done = True
        elif _at_done:
            kept = [s for s in segments if s.type != "at"]
            if not kept:
                continue
            segments = kept
        segments = _ensure_at_trailing_space(segments)

        if _plan_turn is not None:
            _plan_turn.bubbles_sent += 1

        plain_text = strip_mention_tags(block)

        await asyncio.sleep(bubble_pause(len(plain_text)))

        if _deferred_reply is not None and ev is not None:
            _t_seq, _t_mid = _deferred_reply
            _deferred_reply = None
            from gsuid_core.message_history import get_history_manager

            _cur_latest = -1
            for _rec in reversed(get_history_manager().get_history(ev)):
                if _rec.role == "user" and _rec.link is not None:
                    _cur_latest = _rec.link.seq
                    break
            if _cur_latest != -1 and _cur_latest != _t_seq and not any(s.type == "reply" for s in segments):
                segments = [Message("reply", _t_mid), *segments]

        await bot.send(segments, extra_metadata=extra_metadata)
        if _audit is not None:
            _audit(plain_text.strip(), _idx, _total)

    await _send_trailing_artifacts()


async def _send_reply_plan_groups(
    bot: Bot,
    groups: Sequence["ReplyGroup"],
    *,
    turn: "InboxTurn",
    ev: Event | None,
    meta: dict[str, object],
    ooc_check: bool,
) -> None:
    """回复计划分发（§1.6）：每组一次出站，绑定 OutboundBinding 与目标收件人。

    首气泡形态：单目标且发送时刻仍为 A 轨最新 user 记录 → 纯文本；非最新（含打字停顿
    期间有新消息插队）且有 msg_id → 前置 ``Message("reply", msg_id)``；多目标 → 依次
    前置 ``MessageSegment.at(uid)``。
    """
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.message_history.link import OutboundBinding, bind_outbound, reset_outbound
    from gsuid_core.ai_core.evidence.store import get_evidence_store

    pending = frozenset(e.item.seq for e in turn.entries)

    _store = get_evidence_store(turn.session_id)
    _personal_cred = any(
        (o := _store.find(obs_id)) is not None and o.credential_owner is not None and o.credential_owner != "shared"
        for obs_id in turn.observations
    )
    latest_user_seq = -1
    if ev is not None:
        for rec in reversed(get_history_manager().get_history(ev)):
            if rec.role == "user" and rec.link is not None:
                latest_user_seq = rec.link.seq
                break

    for group in groups:
        target_seqs = [s for s in group.seqs]
        if not target_seqs:
            target_seqs = [turn.primary.item.seq]

        targets: list[tuple[str, str]] = []
        for seq in target_seqs:
            uid, uname = "", ""
            entry = turn.entry_for(seq)
            if entry is not None:
                uid = str(entry.event.user_id or "")
                if "nickname" in entry.event.sender:
                    nick = entry.event.sender["nickname"]
                    if isinstance(nick, str):
                        uname = nick.strip()
            elif ev is not None:
                rec = get_history_manager().find_by_seq(ev, seq)
                if rec is not None:
                    uid = str(rec.user_id)
                    uname = rec.user_name or ""
            if uid and all(u != uid for u, _ in targets):
                targets.append((uid, uname))
        if not targets:
            targets.append((str(ev.user_id if ev is not None else ""), ""))

        prefix: list[Message] = []
        reply_target: tuple[int, str] | None = None
        if len(targets) > 1:
            prefix = [MessageSegment.at(uid) for uid, _ in targets]
        else:
            msg_id = ""
            entry = turn.entry_for(target_seqs[0])
            if entry is not None and entry.event.msg_id:
                msg_id = str(entry.event.msg_id)
            elif ev is not None:
                rec = get_history_manager().find_by_seq(ev, target_seqs[0])
                if rec is not None and "msg_id" in rec.metadata:
                    msg_id = str(rec.metadata["msg_id"])
            if target_seqs[0] != latest_user_seq and msg_id:
                prefix = [Message("reply", msg_id)]
            elif msg_id:
                reply_target = (target_seqs[0], msg_id)

        gmeta = dict(meta)
        gmeta["reply_to_user_id"] = targets[0][0]
        if targets[0][1]:
            gmeta["reply_to_user_name"] = targets[0][1]

        binding = OutboundBinding(
            seen_seq=turn.head_seq,
            in_reply_to_seqs=tuple(target_seqs),
            evidence=tuple(turn.observations),
            personal_credential=_personal_cred,
        )
        token = bind_outbound(binding)
        try:
            await send_chat_result(
                bot,
                group.text,
                ev=ev,
                extra_metadata=gmeta,
                ooc_check=ooc_check,
                _reply_prefix=prefix,
                _plan_group=True,
                _reply_target=reply_target,
            )
        finally:
            reset_outbound(token)
        turn.answered |= {s for s in target_seqs if s in pending}


def _split_meme_tag(tag: str) -> tuple[str, str]:
    """第一个词是情绪，其余是场景。空格、中文逗号、英文逗号都能拆。"""
    text = tag.strip().replace("，", " ").replace(",", " ")
    mood, _, scene = text.partition(" ")
    return mood.strip(), scene.strip()


async def _send_meme_from_tag(tag: str, bot: Bot, ev: Event) -> None:
    """解析 meme 标记并发送对应表情包"""
    from gsuid_core.ai_core.outbound import (
        format_meme_placeholder,
        set_outbound_image_label,
        reset_outbound_image_label,
    )
    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply
    from gsuid_core.ai_core.buildin_tools.meme_tools import _get_persona_for_event

    persona = _get_persona_for_event(ev)
    if not persona_allows_meme_reply(persona):
        return

    mood, scene = _split_meme_tag(tag)
    label_token: contextvars.Token | None = None
    try:
        from gsuid_core.ai_core.meme.library import _read_file, get_memes_base_path
        from gsuid_core.ai_core.meme.selector import pick
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        record, reason = await pick(
            mood=mood,
            scene=scene,
            persona=persona,
            session_id=ev.session_id,
        )
        if record is None:
            logger.debug(i18n_t("log.ai.meme_tag_skip", reason=reason, mood=mood, scene=scene))
            return

        file_path = get_memes_base_path() / record.file_path
        if not file_path.exists():
            logger.debug(i18n_t("log.ai.meme_file_exist_path", file_path=file_path))
            return

        image_data = await _read_file(file_path)
        img_b64 = await convert_img(image_data)
        from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

        label_token = set_outbound_image_label(format_meme_placeholder(mood))
        await pause_before_meme()
        await bot.send(MessageSegment.image(img_b64), extra_metadata=turn_reply_metadata(ev))
        await AiMemeRecord.record_usage(record.meme_id, ev.group_id or "")
        logger.info(i18n_t("log.ai.meme_tag_triggered_mood", p0=record.meme_id, mood=mood))
    except Exception as e:
        logger.debug(i18n_t("log.ai.meme_tag_fail", e=e))
    finally:
        if label_token is not None:
            reset_outbound_image_label(label_token)


def _ensure_at_trailing_space(segments: list[Message]) -> list[Message]:
    """确保 at 消息段后紧跟的 text 消息段以空格开头，防止各平台粘连。"""
    result = list(segments)
    for i in range(len(result) - 1):
        if result[i].type == "at" and result[i + 1].type == "text":
            txt = str(result[i + 1].data or "")
            if txt and not txt.startswith((" ", "\t", "\n")):
                result[i + 1] = MessageSegment.text(" " + txt)
    return result


def _parse_at_segments(text: str) -> list[Message]:
    """把 <@用户ID> 解析成 at 段。认转义尖括号和多余空格。裸 @ 留在文本里。"""
    from gsuid_core.ai_core.outbound import (
        is_mention_id,
        iter_mentions,
        rewrite_cq_at,
        normalize_mention_token,
    )

    text = rewrite_cq_at(text)
    segments: list[Message] = []
    last_end = 0

    for match in iter_mentions(text):
        token = normalize_mention_token(match.group(1))
        if not is_mention_id(token):
            continue
        before = text[last_end : match.start()]
        if before:
            segments.append(MessageSegment.text(before))

        segments.append(MessageSegment.at(token))

        last_end = match.end()

    tail = text[last_end:]
    if tail:
        segments.append(MessageSegment.text(tail))

    if not segments:
        return [MessageSegment.text(text)]

    return _ensure_at_trailing_space(segments)
