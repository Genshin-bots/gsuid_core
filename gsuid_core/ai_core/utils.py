import re
import json
import base64
import asyncio
from typing import Any, Dict, Literal, Optional, Sequence

import httpx
from PIL import Image
from json_repair import repair_json
from pydantic_ai.messages import ImageUrl, UserContent

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import Message, MessageSegment
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
