import re
import json
import base64
import asyncio
from typing import Any, Set, Dict, List, Literal, Optional, Sequence

import httpx
from PIL import Image
from json_repair import repair_json
from pydantic_ai.messages import (
    ImageUrl,
    TextPart,
    UserContent,
    ModelMessage,
    ModelRequest,
    ThinkingPart,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
    RetryPromptPart,
    ModelResponsePart,
)
from pydantic_ai.exceptions import ModelHTTPError

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
from gsuid_core.ai_core.const import (
    _RETRYABLE_4XX,
    _CONTENT_REJECT_CODES,
    _CONTENT_REJECT_HINTS,
)
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.resource_manager import RM

# 表情包标记正则：兼容全角/半角冒号，以及前后任意数量的反引号包裹
# 例如：<meme: 困>  `<meme：困>`  ``<meme: 开心>``
MEME_TAG_PATTERN = re.compile(
    r"`*<meme[：:]\s*([^>]+?)>`*",
    re.IGNORECASE,
)

# 模型输出的沉默/控制标记：命中时跳过发送，对话层保持静默
# 所有需要过滤这些标记的地方都应引用此常量，避免散落多处维护不一致
SILENCE_MARKERS: frozenset[str] = frozenset(
    {
        "<SILENCE>",
        "[SILENCE]",
        "SILENCE",
        "<end_turn>",
        "<no_tool_call>",
    }
)

# 工具调用标记残留正则（弱模型 / 兼容网关把工具调用当普通文本输出），
# 详见 _strip_tool_call_artifacts 的 docstring。
# 成对块：含内部 JSON 参数整体删除
_TOOL_CALL_BLOCK_PATTERN = re.compile(
    r"<\s*tool_calls?\s*>.*?<\s*/\s*tool_calls?\s*>",
    re.IGNORECASE | re.DOTALL,
)
# 未闭合起始标签：``<tool_call>`` 后紧跟 { / [，交 _strip_unclosed_tool_call_json 配平定界
_TOOL_CALL_OPEN_TAG_PATTERN = re.compile(
    r"<\s*tool_calls?\s*>\s*(?=[\[{])",
    re.IGNORECASE,
)
# 残留的完整 / 半截标签碎片；缺前括号那条加 \b，避免切到 ``retool_calls>`` 之类词中
_TOOL_CALL_TAG_PATTERN = re.compile(
    r"<\s*/?\s*(?:no_)?tool_calls?\s*>"  # 完整标签 <tool_call> </tool_call> <no_tool_call>
    r"|\b(?:no_)?tool_calls?\s*>"  # 缺前括号碎片 tool_call> / no_tool_call>
    r"|<\s*/?\s*(?:no_)?tool_calls?",  # 缺后括号碎片 <tool_call / </tool_call
    re.IGNORECASE,
)


def _strip_unclosed_tool_call_json(text: str) -> str:
    """删除"未闭合 <tool_call> + JSON 参数"残留，按括号配平定界、不吞后续正文。

    流式截断会留下 ``<tool_call>{partial`` 这类没有闭合标签的起始标签。用
    :func:`_find_json_span` 按括号配平切出 JSON：配平（完整）时只删标签+JSON、保留其后
    正文；不配平（真被截断）时才删到结尾——截断后本就没有后续正文。循环处理多处，每轮
    至少删掉一个起始标签故必然收敛。
    """
    while True:
        m = _TOOL_CALL_OPEN_TAG_PATTERN.search(text)
        if m is None:
            return text
        json_start = m.end()
        span = _find_json_span(text[json_start:])
        if span is None:
            return text
        text = text[: m.start()] + text[json_start + len(span) :]


def _strip_tool_call_artifacts(text: str) -> str:
    """剥离泄漏到普通文本里的工具调用控制标记（及其 JSON 参数）残留。

    弱模型 / OpenAI 兼容网关（MiniMax、部分开源模型）有时不走结构化 function calling，
    而把工具调用以 Hermes/Qwen 风格标签写进文本：``<tool_call>{"name": ...}</tool_call>``；
    网关解析失败或流式分片拆散标签时，整块乃至半截 ``<tool_call>`` / ``</tool_call>`` /
    ``<no_tool_call>`` 就残留在 TextPart 里被原样发往 C 端（与 SILENCE_MARKERS 互补：后者
    只命中整段恰好等于标记，这里处理嵌在文本中的残留）。

    处理顺序：先删成对块（连内部 JSON），再按括号配平删未闭合起始标签+JSON，最后清残留
    碎片。带 ``"tool_call" not in text`` 快路径，正常消息零正则开销。结构化 ToolCallPart 由
    pydantic_ai 单独解析、不经过这里；这里只清"本应是工具调用却以文本泄漏"的残渣。
    """
    if "tool_call" not in text.lower():
        return text
    cleaned = _TOOL_CALL_BLOCK_PATTERN.sub("", text)
    cleaned = _strip_unclosed_tool_call_json(cleaned)
    cleaned = _TOOL_CALL_TAG_PATTERN.sub("", cleaned)
    return cleaned


# 模型私有"回合/角色分隔" token（如 MiniMax 的 ]<]minimax[>[）经 OpenAI 兼容网关
# 透传时偶尔泄漏进正文；新增一类模型的泄漏 token 时在此追加一条正则即可。
_SPECIAL_TOKEN_PATTERNS = (
    # 成对块（含中间角色名）：中段限 ≤32 字 ASCII 标识符，防定界符错配吞掉正文
    re.compile(r"\]<\][A-Za-z0-9_.-]{0,32}\[>\["),
    # 流式截断的半截 token 只会落在文本首/尾：锚定后正文中间的字面串不受影响
    re.compile(r"\A[A-Za-z0-9_.-]{0,32}\[>\[|\]<\][A-Za-z0-9_.-]{0,32}\Z"),
)
# 命中任一特征子串才进正则（正常消息零开销）
_SPECIAL_TOKEN_HINTS = ("]<]", "[>[")


def _strip_special_control_tokens(text: str) -> str:
    """剥离泄漏进正文的模型私有"回合/角色分隔"控制 token（如 MiniMax 的 ``]<]minimax[>[``）。

    与 _strip_tool_call_artifacts（工具调用标记）、SILENCE_MARKERS（整段沉默标记）互补，
    专管说话人/回合分隔符。带快路径：不含特征子串时零正则开销。成对块出现在任意位置都删；
    半截定界符只可能由流式截断产生、必然落在文本首/尾，故第二条正则做了 ``\\A``/``\\Z``
    锚定——正文中间偶然出现的字面 ``]<]`` / ``[>[``（如代码片段、正则示例）不会被误删。
    真正剥离时打 warning，便于追查网关侧泄漏。
    """
    if not any(h in text for h in _SPECIAL_TOKEN_HINTS):
        return text
    cleaned = text
    for pat in _SPECIAL_TOKEN_PATTERNS:
        cleaned = pat.sub("", cleaned)
    if cleaned != text:
        logger.warning(f"[send] 剥离模型私有控制 token 残留（len {len(text)} → {len(cleaned)}）")
    return cleaned


def _find_json_span(text: str) -> Optional[str]:
    """从可能夹带散文/前后缀的文本里，按括号配平切出第一个完整的 JSON 对象或数组。

    比正则 ``\\{.*\\}`` / ``\\[.*\\]`` 更稳：
    - 能正确处理嵌套结构（正则非贪婪会在第一个 ``}`` 处截断、贪婪又会吞掉尾部散文）；
    - 跳过字符串字面量内部的括号与转义，避免把 ``{"msg": "a}b"}`` 里的 ``}`` 当成结束。

    找不到任何 ``{``/``[`` 起点时返回 ``None``；找到起点但括号未配平（输出被截断）时，
    返回从起点到结尾的全部内容，交由 :func:`repair_json` 兜底补全。
    """
    start = next((i for i, ch in enumerate(text) if ch in "{["), None)
    if start is None:
        return None

    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # 起点之后括号始终未配平 → 多半是被截断的输出，整段交给 repair_json 补全
    return text[start:]


def extract_json_from_text(raw_text: str) -> dict | list:
    """从模型自由文本里抽取 JSON，容忍 markdown 围栏与前后散文。

    解析策略（先严后宽，避免 ``repair_json`` 改写本就合法的结构）：
    1. 去掉 markdown 代码围栏（```json ... ``` 或裸 ``` ```）；
    2. 用括号配平从散文里切出第一个完整 JSON（剥掉"好的，结果如下："这类寒暄/解释）；
    3. 快路径：对候选串直接 ``json.loads``；
    4. 慢路径：``repair_json`` 兜底（单引号 / 尾随逗号 / 漏引号 / 被截断的尾部等）。
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Empty input text for JSON extraction")

    # 过滤已知的非 JSON 特殊标记（如模型输出的 <SILENCE>）
    stripped = raw_text.strip()
    if stripped in SILENCE_MARKERS:
        raise ValueError(f"Special marker '{stripped}' is not valid JSON")

    # 上游 agent 出错时会返回 "执行出错: ..." 之类的字符串，这里提前拦截
    if stripped.startswith("执行出错"):
        raise ValueError(f"Upstream agent returned error message, not JSON: {stripped[:80]}")

    # 去掉 markdown 代码围栏：开围栏可带任意语言标注（```json / ```JSON / ``` ），统一剥掉
    cleaned = re.sub(r"```[a-zA-Z]*\s*", "", raw_text)
    cleaned = cleaned.replace("```", "").strip()
    if not cleaned:
        raise ValueError("JSON extraction yielded empty content after stripping fences")

    # 候选串：① 从散文里配平切出的第一个完整 JSON；② 去围栏后的整段。先 span 后整段，
    # 让 span 优先剥掉模型在 JSON 前后写的解释/寒暄；两者都试以兼容纯 JSON 与夹带散文两种返回。
    span = _find_json_span(cleaned)
    candidates = [c for c in (span, cleaned) if c]

    # 快路径：合法 JSON 直接 loads，不经 repair（json_repair 偶尔会改写本就合法的结构）
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue

    # 慢路径：repair_json 兜底容错；它对无法修复的输入返回空串，过滤掉后再 loads
    for cand in candidates:
        try:
            repaired = repair_json(cand)
        except Exception:  # repair_json 理论上不抛，仍兜底防御
            continue
        if not repaired or not repaired.strip():
            continue
        try:
            return json.loads(repaired)
        except (json.JSONDecodeError, ValueError):
            continue

    raise ValueError(f"Failed to parse JSON from text: {stripped[:120]!r}")


async def handle_tool_result(bot: Optional[Bot], result: Any, max_length: int = 4000) -> str:
    """
    序列化工具执行结果, 当函数返回Message对象时调用Bot.send方法发送, 并将序列化后的字符串返回方便AI识别。

    Args:
        bot: Bot 对象
        result: 工具函数返回的结果
        max_length: 最大返回长度，超长会被截断

    Returns:
        序列化的字符串
    """
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
    elif hasattr(result, "model_dump_json"):
        # Pydantic v2
        res_str = result.model_dump_json()
    elif hasattr(result, "json"):
        # Pydantic v1
        res_str = result.json()
    else:
        res_str = str(result)

    # 截断过长的返回值，防止 Token 爆炸
    if len(res_str) > max_length:
        return res_str[:max_length] + f"\n...[系统截断: 省略后 {len(res_str) - max_length} 字符]"
    return res_str


def _normalize_image_url(raw: str) -> str:
    """将各种图片格式统一转为可消费的 URL（HTTP 或 DataURI）

    Args:
        raw: 原始图片标识，支持 http/https URL、base64:// 前缀、data:image/ 前缀、裸 base64

    Returns:
        标准化的图片 URL
    """
    if raw.startswith(("http", "https")):
        return raw
    if raw.startswith("base64://"):
        return f"data:image/png;base64,{raw[10:]}"
    if raw.startswith("data:image/"):
        return raw
    return f"data:image/png;base64,{raw}"


def _guess_image_mime(url: str) -> str:
    """按 URL 扩展名兜底推断图片 MIME，识别不出时默认 image/jpeg。"""
    u = url.lower()
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".gif"):
        return "image/gif"
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    return "image/jpeg"


async def materialize_image_url(raw: str, *, strict: bool = False) -> str:
    """把图片标识统一物化为「不会过期」的可消费形式。

    远程 http(s) 图片 URL（典型如 QQ 带 ``rkey`` 的临时链接）有时效，过期后
    推理端再去下载就会返回 500「Failed to download image」。而这些 URL 会被
    写进 pydantic_ai 的 ``message_history`` 并在之后每一轮重发——一旦过期，整个
    会话会被反复 500 卡死。故在「入历史前」就把远程 URL 下载并编码为 base64
    DataURI（内联数据，永不过期）。

    - http(s) URL：下载 → ``data:<mime>;base64,<...>``；下载失败时回退原 URL
      （不致命，行为不差于改动前）。
      当 ``strict=True`` 时下载失败会抛出异常而非静默回退，供调用方显式处理。
    - base64:// / data:image/ / 裸 base64：交给 :func:`_normalize_image_url`
      处理即可，本就不会过期，无需下载。

    Args:
        raw: 原始图片标识。
        strict: 是否严格模式。为 True 时下载失败抛出 RuntimeError，
                为 False（默认）时下载失败回退原始 URL。
    """
    if not raw.startswith(("http://", "https://")):
        return _normalize_image_url(raw)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(raw)
            resp.raise_for_status()
            data = resp.content
            mime = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not mime.startswith("image/"):
            mime = _guess_image_mime(raw)
        b64 = base64.b64encode(data).decode("ascii")
        logger.debug(f"🖼️ [GsCoreAI] 远程图片已物化为 base64 DataURI ({mime}, {len(data)} bytes)")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        if strict:
            raise RuntimeError(f"远程图片下载失败，无法物化为 base64: {raw[:120]} ({e})") from e
        logger.warning(f"🖼️ [GsCoreAI] 远程图片转 base64 失败，回退原始 URL: {e}")
        return raw


def _is_master_user(user_id: str) -> bool:
    """判断指定用户是否为机器人主人"""
    try:
        from gsuid_core.config import core_config

        masters = core_config.get_config("masters") or []
        return str(user_id) in [str(m) for m in masters]
    except Exception:
        return False


def _build_relationship_description(
    favorability: Optional[int],
    user_name: Optional[str],
    user_id: str,
) -> str:
    """将好感度转换为有温度的关系描述，而非机械的区间标签。

    群聊场景下整个群共用一个 session，多人轮流发言。因此说话者描述里
    **必须显式带上用户ID**，否则昵称重复或为"我"这类无意义值时，
    Agent 无法区分到底是谁在说话。
    """
    # 说话者标识：始终包含用户ID，昵称仅作辅助
    if user_name and user_name.strip() and user_name.strip() != str(user_id):
        speaker = f"{user_name.strip()}(用户ID:{user_id})"
    else:
        speaker = f"用户ID:{user_id}"

    # 主人用户：显著高亮，提示角色以最高信任度对待
    if _is_master_user(user_id):
        return f"【⚡ 你的主人】{speaker} 直接找你说话了。对主人：完全信任，认真对待，有求必应（合规范围内）。"

    if favorability is None:
        return f"{speaker} 找你说话了。"

    if favorability < 0:
        return f"{speaker} 又来了。"
    elif favorability < 20:
        return f"{speaker} 来找你了，你们不太熟。"
    elif favorability < 50:
        return f"{speaker} 找你说话，见过几次面的那种。"
    elif favorability < 75:
        return f"{speaker} 找你了，算是熟人了。"
    else:
        return f"{speaker} 找你说话了，你们挺熟的。"


async def prepare_content_payload(
    ev: Event,
    task_level: Literal["high", "low"] = "high",
    favorability: Optional[int] = None,
    favorability_zone: Optional[str] = None,
) -> Sequence[UserContent]:
    """
    准备消息内容列表给AI看, 包含文本、图片ID、文件内容、事件对象

    图片处理分两种模式（由 ai_config ``lazy_image_read`` 开关决定）：
    - 惰性投喂（默认开启）：图片本体不进上下文，只把图片ID以文字透传给 AI；
      AI 需要看图时再调用 ``read_image(图片ID)`` 按需读取。群聊图片多时显著省 Token。
    - 直接投喂（关闭时，旧行为）：把图片物化为 ImageUrl 放进 content payload，
      再由 ``GsCoreAIAgent._execute_run`` 处理（模型支持图片时直接传图，
      不支持时通过 understand_image 转述为文字）。

    Args:
        ev: 事件对象
        task_level: 任务级别
        favorability: 当前用户好感度 (可选)
        favorability_zone: 好感度区间描述 (可选)

    Returns:
        content payload 列表（惰性模式仅含文本；直接模式可能含 ImageUrl）
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    # 惰性图片投喂开关：群聊图片多时, 默认只透传图片ID, 由 AI 按需 read_image 读图,
    # 避免一次性把大量图片塞进多模态上下文导致 Token 爆炸 / 注意力被稀释。
    lazy_image_read = bool(ai_config.get_config("lazy_image_read").data)

    content_payload: list[UserContent] = []

    # 获取用户昵称
    nickname = None
    if ev.sender:
        nickname = ev.sender.get("nickname") or ev.sender.get("card") or None

    # 叙事性关系描述（Bug-01 + Prompt-2.2: 替代数字+区间标签）
    relationship_desc = _build_relationship_description(favorability, nickname, str(ev.user_id))
    current_turn_header = f"{relationship_desc}\n"

    # @状态：只在被@时才注入（潜在-01: 修正 is_at_me → is_tome）
    is_at_me = getattr(ev, "is_tome", False) or (ev.user_type == "direct")
    if is_at_me:
        current_turn_header += "（直接找你说的）\n"

    current_turn_header += "--- 消息 ---\n"

    text = current_turn_header
    if not ev.text:
        text += "用户没有发送文本内容。"
    else:
        text += ev.text.strip()

    # 预处理, 将用户发送的文本/AT/图片ID/音频ID等信息整合到一个字符串中, 方便AI处理
    if ev.image_id_list:
        if lazy_image_read:
            # 惰性模式：只给图片ID + 一行调用指引，图片本体留在 RM，需要看时再 read_image
            text += "\n--- 用户发送了图片(未展开, 需要查看内容时调用 read_image(图片ID)) ---"
            for i in ev.image_id_list:
                text += f"\n图片ID: {i}"
            text += "\n"
        else:
            for i in ev.image_id_list:
                text += f"\n--- 用户上传图片ID: {i} ---\n"

    if hasattr(ev, "audio_id") and ev.audio_id:
        text += f"\n--- 用户上传音频ID: {ev.audio_id} ---\n"

    for i in getattr(ev, "audio_id_list", []):
        text += f"\n--- 用户上传音频ID: {i} ---\n"

    for at in ev.at_list:
        text += f"\n--- 提及用户(@用户): {at} ---\n"

    content_payload.append(text)

    # 惰性模式：图片只以 ID 形式存在（已在上方文本注明），不把本体喂进多模态上下文，
    # 由 AI 调用 read_image 按需读取。直接跳过下方的图片物化。
    if lazy_image_read:
        return content_payload

    # Fix-07: 收到消息时立即物化远程图片 URL，避免过期后写入历史。
    # 远程 URL（如 QQ 带 rkey 的临时链接）会在短时间内过期；一旦以原始
    # URL 形式存入 message_history，后续每轮重发都会让推理端 400/500。
    for i in ev.image_list:
        if isinstance(i, str):
            # strict=True：远程图片下载失败直接抛出，跳过该图片而非把过期 URL 塞进历史
            try:
                url = await materialize_image_url(i, strict=True)
            except Exception as e:
                logger.warning(f"🖼️ [GsCoreAI] 图片物化失败（URL 可能已过期），跳过图片: {i[:120]} ({e})")
                continue
            content_payload.append(ImageUrl(url=url))
        else:
            logger.warning(f"无法处理图片ID: {i}")

    return content_payload


def _looks_like_tool_table(text: str) -> bool:
    """检测文本是否为工具结果 / 数据展示（含 markdown 表格或代码块）。

    用于在 ``send_chat_result`` 中豁免 markdown 净化——闲聊回复剥离 markdown，
    但工具检索结果（如表格）需保留原样。
    """
    if "```" in text:
        return True
    # 含表格分隔行 |---| 或多个表格竖线时视为表格
    if re.search(r"\|.*\|.*\|", text) and ("---" in text or text.count("|") >= 4):
        return True
    return False


def _strip_persona_markdown(text: str) -> str:
    """剥离闲聊/人格回复里的 markdown 与 ``*动作*`` 旁白（B-2）。

    QQ 等 IM 不渲染 markdown，字面的 ``**加粗**`` / ``*揉揉眼睛*`` 极不拟人。
    但**工具结果**（含表格 / 代码块）需保留原样以便阅读，故命中
    ``_looks_like_tool_table`` 时原样返回、不做任何剥离。
    """
    if _looks_like_tool_table(text):
        return text
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)  # **x** / *x* → x
    # 整行舞台旁白：整行仅一个 （…） 且括号内 ≥4 字（小说式动作/神态描写），连换行一起删。
    # 阈值 4 放过 （笑）（误）（脸红） 这类真·口语 tone，只清"（眼睛弯成月牙）"式叙事旁白。
    text = re.sub(r"(?m)^[ \t]*[（(][^（）()]{4,}[）)][ \t]*\n?", "", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)  # 标题
    text = re.sub(r"^\s{0,3}[-*>]\s+", "", text, flags=re.M)  # 列表 / 引用
    return text


async def send_chat_result(
    bot: Bot,
    text: str,
    ev: Event | None = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    解析并发送聊天结果，支持：
    - 按换行分割多条消息
    - @用户ID 语法 → MessageSegment.at(user_id)
    - <meme: 情绪> 标记（可带反引号）→ 触发表情包发送（需传入 ev）
    - extra_metadata：透传到 ``Bot.send`` 的 ``extra_metadata``，最终落到
      ``message_history`` 记录上（如主动消息的 ``proactive=True / source / reason``）
    """
    if not text:
        return

    # 过滤模型输出的特殊控制标记（如 <end_turn>），避免发送给用户
    _trimmed = text.strip()
    if _trimmed in SILENCE_MARKERS:
        logger.debug(f"[send_chat_result] 跳过特殊标记: {_trimmed!r}")
        return

    # 最终边界守卫：剥离泄漏到文本里的工具调用标记残留（详见 _strip_tool_call_artifacts）
    # 与模型私有的回合/角色分隔特殊 token（如 MiniMax 的 ]<]minimax[>[，详见
    # _strip_special_control_tokens）。覆盖前摇 / 兜底总结 / 主动消息 / 子 Agent 转述等
    # 所有经本函数下发的路径。
    text = _strip_tool_call_artifacts(text)
    text = _strip_special_control_tokens(text)

    # Trace 日志：记录原始输出
    logger.trace(f"[Meme] 原始输出: {text!r}")

    # 解析表情包标记
    meme_tags: list[str] = MEME_TAG_PATTERN.findall(text)
    clean_text: str = MEME_TAG_PATTERN.sub("", text).strip()

    # 闲聊/人格回复剥离 markdown 与 *动作* 旁白（工具表格/代码块自动豁免，见该函数）。
    clean_text = _strip_persona_markdown(clean_text)

    # 清理标记残留的多余空格/标点。只压"空格/制表符"、保留换行——原 \s{2,} 会把
    # \n\n 也压成空格，导致下方 re.split(r"\n\s*\n") 切不出多条，"连发多条短句"退化成一整段。
    clean_text = re.sub(r"[ \t]{2,}", " ", clean_text)
    clean_text = re.sub(r"^[，。！？\s]+|[，。！？\s]+$", "", clean_text)

    # Trace 日志：记录解析结果
    logger.trace(f"[Meme] 解析标记: {meme_tags}, 清理后文本: {clean_text!r}")

    if not clean_text:
        # 没有纯文本时，如果有表情包标记且有 ev，直接发图片
        if meme_tags and ev is not None:
            await _send_meme_from_tag(meme_tags[0].strip(), bot, ev)
        return

    # 按换行分割为多条消息
    blocks = re.split(r"\n\s*\n", clean_text)

    for block in blocks:
        if not block.strip():
            continue

        segments = _parse_at_segments(block)

        # 计算纯文本长度
        plain_text = re.sub(r"@\d+", "", block)

        # 模拟打字延迟
        delay = min(max(len(plain_text) / 7, 0.5), 3.0)
        await asyncio.sleep(delay)

        await bot.send(segments, extra_metadata=extra_metadata)

    # 发送表情包（如有）
    if meme_tags and ev is not None:
        await _send_meme_from_tag(meme_tags[0].strip(), bot, ev)


async def _send_meme_from_tag(mood: str, bot: Bot, ev: Event) -> None:
    """解析 meme 标记并发送对应表情包"""
    from gsuid_core.ai_core.meme.config import meme_config

    if not meme_config.get_config("meme_enable").data:
        return

    try:
        from gsuid_core.ai_core.meme.library import _read_file, get_memes_base_path
        from gsuid_core.ai_core.meme.selector import pick
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord
        from gsuid_core.ai_core.buildin_tools.meme_tools import _get_persona_for_event

        persona = _get_persona_for_event(ev)
        record, _reason = await pick(
            mood=mood,
            scene="",
            persona=persona,
            session_id=ev.session_id,
        )
        if record is None:
            return

        file_path = get_memes_base_path() / record.file_path
        if not file_path.exists():
            logger.debug(f"[Meme] 表情包文件不存在: {file_path}")
            return

        image_data = await _read_file(file_path)
        img_b64 = await convert_img(image_data)
        await bot.send(MessageSegment.image(img_b64))
        await AiMemeRecord.record_usage(record.meme_id, ev.group_id or "")
        logger.info(f"[Meme] 标记触发表情包: {record.meme_id} (mood={mood})")
    except Exception as e:
        logger.debug(f"[Meme] 标记发送失败: {e}")


def _parse_at_segments(text: str) -> list[Message]:
    """
    将含有 @用户ID 的文本解析为 MessageSegment 列表。

    规则：
    - @后跟纯数字（QQ号格式）才会被解析为 at segment
    - 其余文本保持为 text segment
    - 示例输入："好哦 @444835641 你来看"
    - 示例输出：[Text("好哦 "), At(444835641), Text(" 你来看")]
    """
    # 匹配 @数字，前后允许空格（空格属于分隔符，不计入文本内容）
    pattern = re.compile(r"\s*@(\d+)\s*")
    segments: list[Message] = []
    last_end = 0

    for match in pattern.finditer(text):
        # 匹配前的普通文本
        before = text[last_end : match.start()]
        if before:
            segments.append(MessageSegment.text(before))

        # @ 片段
        user_id = match.group(1)
        segments.append(MessageSegment.at(user_id))

        last_end = match.end()

    # 剩余文本
    tail = text[last_end:]
    if tail:
        segments.append(MessageSegment.text(tail))

    # 如果没有任何 @ 匹配，直接返回原始字符串（兼容旧调用）
    if not segments:
        return [MessageSegment.text(text)]

    return segments


# ======================================================================
# GsCoreAIAgent 运行期的无状态消息/历史工具
# 从 gs_agent.py 抽出：纯函数，只依赖 pydantic_ai 消息类型与 const 常量，不触碰
# Agent 实例状态，便于复用与单测。gs_agent 按原名 import 回去使用。
# ======================================================================


def _is_non_retryable_model_error(e: BaseException) -> bool:
    """该异常是否为"重试也必然复现"的永久性模型错误（4xx 客户端错误，排除 408/429）。"""
    return isinstance(e, ModelHTTPError) and 400 <= e.status_code < 500 and e.status_code not in _RETRYABLE_4XX


def _is_content_rejected(e: ModelHTTPError) -> bool:
    """4xx 错误是否为"内容被模型安全 / 审核策略拒绝"（用于更友好的文案与统计分类）。"""
    blob = (str(e.body or "") + " " + str(e)).lower()
    if any(hint in blob for hint in _CONTENT_REJECT_HINTS):
        return True
    return any(re.search(rf"\b{code}\b", blob) for code in _CONTENT_REJECT_CODES)


def _extract_run_context(history: List[ModelMessage], max_fact_len: int = 2000) -> str:
    """从历史消息中提取"已知事实"和"模型推理片段"，按轮次组织。

    相比只提取 ToolReturnPart，还保留 TextPart（LLM 中间推理），
    因为这些推理有时本身就是有价值的结论。
    """
    sections: list[str] = []
    round_num = 0

    for msg in history:
        if isinstance(msg, ModelResponse):
            round_num += 1
            texts: list[str] = []
            calls: list[str] = []
            for part in msg.parts:
                if isinstance(part, TextPart) and part.content.strip():
                    t = part.content.strip()
                    if len(t) > 500:
                        t = t[:500] + "...[截断]"
                    texts.append(t)
                elif isinstance(part, ToolCallPart):
                    calls.append(part.tool_name)

            if texts or calls:
                header = f"【第{round_num}轮】"
                if calls:
                    header += f" 调用工具: {', '.join(calls)}"
                if texts:
                    header += "\n" + "\n".join(texts)
                sections.append(header)

        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    content = str(part.content).strip()
                    if len(content) > max_fact_len:
                        content = content[:max_fact_len] + f"\n...[截断, 共{len(content)}字符]"
                    sections.append(f"  → [{part.tool_name}] 返回: {content}")

    return "\n".join(sections) if sections else ""


def _truncate_message_for_log(msg: Any, max_base64_len: int = 100) -> Any:
    """
    截断消息中的长 base64 数据，用于日志输出。

    Args:
        msg: 消息内容，可能是 str、ImageUrl 或其列表
        max_base64_len: base64 数据最大显示长度

    Returns:
        截断后的消息副本
    """
    if isinstance(msg, str):
        # 检查是否是 base64 DataURI
        if ";base64," in msg and len(msg) > max_base64_len:
            return f"{msg[:max_base64_len]}...[base64截断, 总长={len(msg)}]"
        return msg
    elif isinstance(msg, ImageUrl):
        url = msg.url
        if ";base64," in url and len(url) > max_base64_len:
            return ImageUrl(url=f"{url[:max_base64_len]}...[base64截断, 总长={len(url)}]")
        return msg
    elif isinstance(msg, list):
        return [_truncate_message_for_log(item, max_base64_len) for item in msg]
    return msg


def _truncate_history_with_tool_safety(
    history: List[ModelMessage],
    max_history: int,
) -> List[ModelMessage]:
    """
    安全截断 history，确保保留的消息中 ToolCallPart 和 ToolReturnPart 完全配对。

    问题：如果简单地从末尾截断 history，可能导致 ToolReturnPart 被保留
    但其对应的 ToolCallPart 被丢弃（在被截断的前半部分），从而在下一轮请求时出现
    "tool result's tool id not found" 错误。

    解决策略：
    1. 先做一次试探性截断：保留最后 max_history 条消息
    2. 扫描截断结果，收集所有保留的 ToolReturnPart 的 tool_call_id
    3. 扫描截断结果，收集所有保留的 ToolCallPart 的 tool_call_id
    4. 如果有 return 找不到对应的 call，说明截断点切到了 tool call/return 对的中间
    5. 向前移动截断点，直到所有保留的 return 都有对应的 call

    Args:
        history: 原始消息历史
        max_history: 最大保留消息数

    Returns:
        截断后的安全消息历史
    """
    if len(history) <= max_history:
        return history

    # 从 max_history 开始，逐步扩大保留范围，直到 tool call/return 完全配对
    truncate_index = len(history) - max_history

    while truncate_index > 0:
        truncated = history[truncate_index:]

        # 收集截断结果中所有 ToolCallPart 的 tool_call_id
        retained_call_ids: Set[str] = set()
        # 收集截断结果中所有 ToolReturnPart 的 tool_call_id
        retained_return_ids: Set[str] = set()

        for msg in truncated:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        retained_call_ids.add(part.tool_call_id)
            elif isinstance(msg, ModelRequest):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        retained_return_ids.add(part.tool_call_id)
                    # RetryPromptPart 也是"工具结果型"消息：工具参数校验失败时
                    # 由 PydanticAI 生成，同样带 tool_call_id、必须有配对的
                    # ToolCallPart。tool_name 为 None 时是输出校验重试，不绑定
                    # 具体工具调用，不计入。
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        retained_return_ids.add(part.tool_call_id)

        # 找出截断结果中的孤立 return（有 return 但没有对应的 call）
        orphaned = retained_return_ids - retained_call_ids

        if not orphaned:
            # 所有保留的 return 都有对应的 call，截断安全
            logger.debug(
                f"🧠 [GsCoreAIAgent] 安全截断 history: {len(history)} -> {len(truncated)} (截断点: {truncate_index})"
            )
            return truncated

        # 有孤立 return，需要向前移动截断点
        # 找到所有孤立 return 所在的消息索引（相对于原始 history）
        min_orphaned_idx = len(history)  # 初始化为最大值
        for idx, msg in enumerate(history):
            if idx < truncate_index:
                continue  # 只看截断范围内的
            if isinstance(msg, ModelRequest):
                for part in msg.parts:
                    tcid: Optional[str] = None
                    if isinstance(part, ToolReturnPart):
                        tcid = part.tool_call_id
                    elif isinstance(part, RetryPromptPart) and part.tool_name is not None:
                        tcid = part.tool_call_id
                    if tcid is not None and tcid in orphaned:
                        min_orphaned_idx = min(min_orphaned_idx, idx)

        # 向前移动截断点到孤立 return 之前，再留 2 条消息的缓冲
        new_truncate_index = max(0, min_orphaned_idx - 2)
        if new_truncate_index >= truncate_index:
            # 安全阀：如果无法继续前移，直接保留全部历史
            logger.warning(f"🧠 [GsCoreAIAgent] 无法安全截断 history，保留全部 {len(history)} 条")
            return history

        truncate_index = new_truncate_index

    # truncate_index == 0，保留全部历史
    logger.debug(f"🧠 [GsCoreAIAgent] 安全截断 history: {len(history)} -> {len(history)} (保留全部)")
    return history


def _drop_orphan_tool_results(history: List[ModelMessage]) -> List[ModelMessage]:
    """丢弃所有找不到配对 ToolCallPart 的孤儿工具结果消息。

    最终一致性兜底：即便 ``_truncate_history_with_tool_safety`` 逻辑正确，
    历史里仍可能因并发 / 异常中断残留坏配对（孤儿 ToolReturnPart 或带
    tool_name 的 RetryPromptPart）。本函数在 ``extract_history()`` 末尾被
    无条件调用，保证送进 API 的 message_history 永远自洽——一次坏截断不会
    让 session 永久不可用（"tool result's tool id not found" 400）。
    """
    call_ids: Set[str] = set()
    for msg in history:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)

    cleaned: List[ModelMessage] = []
    for msg in history:
        if isinstance(msg, ModelRequest):
            kept_parts = []
            for part in msg.parts:
                # 复用同一个 isinstance 守卫：进入分支时 part 类型已被 mypy/Pyright
                # 收窄为 ToolReturnPart / RetryPromptPart，两者都有 tool_call_id，
                # 不需要 getattr 兜底（LLM.md §1.4）。
                if isinstance(part, ToolReturnPart) and part.tool_call_id not in call_ids:
                    logger.warning(f"🧠 [GsCoreAIAgent] 丢弃孤儿 ToolReturnPart: tool_call_id={part.tool_call_id}")
                    continue
                if (
                    isinstance(part, RetryPromptPart)
                    and part.tool_name is not None
                    and part.tool_call_id not in call_ids
                ):
                    logger.warning(f"🧠 [GsCoreAIAgent] 丢弃孤儿 RetryPromptPart: tool_call_id={part.tool_call_id}")
                    continue
                kept_parts.append(part)
            if kept_parts:
                msg.parts = kept_parts
                cleaned.append(msg)
            # parts 全被丢弃的空 ModelRequest 整条剔除
        else:
            cleaned.append(msg)
    return cleaned


def _strip_remote_images_from_history(history: List[ModelMessage]) -> int:
    """把历史里残留的「远程图片 URL」剥离成文字占位，返回剥离处数量。

    推理端报「Failed to download image」基本都是早先入历史的远程图片 URL
    （如 QQ 带 rkey 的临时链接）已过期。不清掉的话，后续每一轮把它重发给
    推理端都会 500，整个会话被永久卡死。这里把过期的远程 ``ImageUrl`` 替换为
    文字占位，让下一轮自动恢复。base64 DataURI 不会过期，保留不动。
    """
    removed = 0
    for msg in history:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content
            if isinstance(content, str):
                continue
            new_content: List[UserContent] = []
            changed = False
            for item in content:
                if isinstance(item, ImageUrl) and item.url.startswith(("http://", "https://")):
                    new_content.append("[图片已过期，无法再显示]")
                    changed = True
                    removed += 1
                else:
                    new_content.append(item)
            if changed:
                part.content = new_content
    return removed


def _split_embedded_thinking(
    parts: Sequence[ModelResponsePart],
    thinking_tags: tuple[str, str],
) -> List[ModelResponsePart]:
    """把 TextPart 里以 thinking_tags 包裹的内嵌思考重新拆成独立的 ThinkingPart。

    流式请求下 pydantic_ai 只有在 ``<think>`` 作为独立 SSE chunk 到达时才会拆分思考
    标签（见 _parts_manager.handle_text_delta）；MiniMax 等兼容网关不保证这一点，
    导致 ``<think>...</think>`` 残留在 TextPart 里被原样发往 C 端，且思考内容拿不到
    意图-行为一致性检测。这里按完整文本重新拆分，对齐非流式路径
    （openai 模型的 split_content_into_text_and_thinking）的行为。非 TextPart 与不含
    起始标签的 TextPart 原样透传。
    """
    start_tag, end_tag = thinking_tags
    result: List[ModelResponsePart] = []
    for part in parts:
        if not isinstance(part, TextPart) or start_tag not in part.content:
            result.append(part)
            continue
        content = part.content
        start_index = content.find(start_tag)
        while start_index >= 0:
            before, content = content[:start_index], content[start_index + len(start_tag) :]
            if before:
                result.append(TextPart(content=before))
            end_index = content.find(end_tag)
            if end_index >= 0:
                think, content = content[:end_index], content[end_index + len(end_tag) :]
                result.append(ThinkingPart(content=think))
            else:
                # 缺少闭合标签：丢弃 <think> 起始标签，剩余内容按文本处理
                result.append(TextPart(content=content))
                content = ""
            start_index = content.find(start_tag)
        if content:
            result.append(TextPart(content=content))
    return result


def _sanitize_tool_call_artifacts_in_parts(
    parts: Sequence[ModelResponsePart],
) -> List[ModelResponsePart]:
    """清除各 TextPart 泄漏的工具调用标记残留，并丢弃被清空的 TextPart。

    与 :func:`_split_embedded_thinking` 配套，在其拆出内嵌 thinking 之后调用。整体替换
    ``node.model_response.parts``，使 history、result.output、下发文本三处一并保持干净——
    避免"这条没发但 history 里留着 <tool_call>，诱导模型下一轮继续这么输出"。

    被清理为空的 TextPart 直接丢弃、不留空串污染 history；但当丢弃会使整条响应不剩任何
    part 时（响应整段就是泄漏的工具调用），保留一个空 TextPart 占位，规避部分网关
    "assistant 消息必须有内容"的报错。ToolCallPart / ThinkingPart 原样透传。
    """
    kept: List[ModelResponsePart] = []
    for part in parts:
        if isinstance(part, TextPart):
            cleaned = _strip_special_control_tokens(_strip_tool_call_artifacts(part.content))
            if cleaned != part.content:
                if not cleaned.strip():
                    continue  # 整段都是残留 → 丢弃，不留空串
                part.content = cleaned
        kept.append(part)
    if not kept:
        return [TextPart(content="")]
    return kept
