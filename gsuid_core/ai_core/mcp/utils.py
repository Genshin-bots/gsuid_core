"""
MCP 工具复用函数模块

将各业务模块（web_search / image_understand / asr / document / video）
中重复出现的 MCP 模式统一抽象为可复用函数，减少样板代码。

典型用法:
    # 1. 获取并校验 MCP 工具 ID
    mcp_tool_id = get_mcp_tool_id("asr_mcp_tool_id", "ASR")

    # 2. 保存二进制数据到临时文件
    audio_path = await save_binary_to_tempfile(audio_data, ".ogg", "🎤 [ASR]")

    # 3. 调用 MCP 工具（自动校验错误）
    result = await call_mcp_tool_checked(mcp_tool_id, arguments, "ASR")

    # 4. 清理临时文件
    cleanup_tempfile(audio_path, "🎤 [ASR]")

    # 5. 解析 MCP 返回的二进制数据（DataURI / base64 / 文件路径）
    audio_bytes = await parse_binary_result(result.text, media_type="audio")
"""

import os
import re
import base64
import tempfile
from typing import Any, Optional

import aiofiles

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.client import MCPToolResult
from gsuid_core.ai_core.mcp.mcp_tool_caller import call_mcp_tool
from gsuid_core.utils.plugins_config.models import GsStrConfig
from gsuid_core.ai_core.mcp.mcp_tools_config import mcp_tools_config

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MCP_PROVIDER = "MCP"
"""MCP 提供方标识符，各业务模块通过 `if provider == MCP_PROVIDER` 判断"""


# ---------------------------------------------------------------------------
# 1. 获取 & 校验 MCP 工具 ID
# ---------------------------------------------------------------------------


def get_mcp_tool_id(config_key: str, feature_name: str) -> str:
    """从 mcp_tools_config 获取并校验 MCP 工具 ID。

    各业务模块在使用 MCP 前都需要先读取配置的 tool_id，若未配置则抛出
    RuntimeError 提示用户前往设置页面。

    Args:
        config_key: mcp_tools_config 中的配置键名，
            如 ``"asr_mcp_tool_id"``、``"document_extract_mcp_tool_id"``
        feature_name: 功能中文名，用于错误提示，
            如 ``"ASR"``、``"Document Extract"``、``"Web Search"``

    Returns:
        MCP 工具 ID 字符串，格式为 ``"{mcp_id} - {tool_name}"``

    Raises:
        RuntimeError: 配置键对应的值为空时抛出

    Example:
        >>> tool_id = get_mcp_tool_id("asr_mcp_tool_id", "ASR")
        >>> # tool_id = "minimax - asr"
    """
    mcp_tool_id = get_mcp_tool_id_optional(config_key)

    if not mcp_tool_id:
        raise RuntimeError(
            t(
                "{feature_name} MCP 工具未配置，请前往 AI 配置页面设置 {config_key}",
                feature_name=feature_name,
                config_key=config_key,
            )
        )

    return mcp_tool_id


def get_mcp_tool_id_optional(config_key: str) -> str:
    """获取 MCP 工具 ID，未配置时返回空字符串而非抛异常。

    供「工具可选、未配置则回退到其它方案」的场景使用（如视频理解：未配置直连工具时
    回退到关键帧提取 + 图片理解）。需要「未配置即报错」的强制场景请用 get_mcp_tool_id。

    Args:
        config_key: mcp_tools_config 中的配置键名

    Returns:
        MCP 工具 ID 字符串；未配置时为 ""
    """
    config_item = mcp_tools_config.get_config(config_key)
    if isinstance(config_item, GsStrConfig):
        return config_item.data
    return ""


# ---------------------------------------------------------------------------
# 1.1 获取 MCP 工具的 details 参数映射
# ---------------------------------------------------------------------------

# 映射值前缀，标识从内部参数取值
_PARAMS_PREFIX = "params - "


def get_mcp_tool_details(config_key: str) -> Optional[dict[str, Any]]:
    """获取 mcp_tools_config 中指定配置键的 details 参数映射。

    Args:
        config_key: mcp_tools_config 中的配置键名，
            如 ``"websearch_mcp_tool_id"``

    Returns:
        details 字典，若未配置则返回 None

    Example:
        >>> details = get_mcp_tool_details("websearch_mcp_tool_id")
        >>> # {"query": "params - query", "max_results": "params - max_results"}
    """
    config_item = mcp_tools_config.get_config(config_key)
    # 仅 GsStrConfig 带 details 字段；用 isinstance 守卫而非 getattr 兜底
    if isinstance(config_item, GsStrConfig) and config_item.details:
        return config_item.details
    return None


# ---------------------------------------------------------------------------
# 1.2 构建 MCP 工具参数（根据 details 映射）
# ---------------------------------------------------------------------------


def build_mcp_arguments(
    config_key: str,
    internal_params: dict[str, Any],
) -> dict[str, Any]:
    """根据 details 参数映射构建 MCP 工具调用参数。

    不同的 MCP 工具对外提供的参数名不同，而框架内部函数的参数名是固定的。
    此函数通过 details 配置将内部参数名映射为 MCP 工具期望的参数名，
    同时支持传入固定值（字面量）。

    **details 值的格式规则:**
    - ``"params - <内部参数名>"`` → 从 internal_params 中取对应键的值
    - 字面量 (str / int / float / bool) → 直接作为固定值传入
    - ``None`` → 跳过，不传该参数

    Args:
        config_key: mcp_tools_config 中的配置键名，
            如 ``"websearch_mcp_tool_id"``
        internal_params: 内部函数的参数字典，
            键为内部参数名，值为实际传入的值

    Returns:
        构建好的 MCP 工具参数字典，可直接传给 ``call_mcp_tool_checked``

    Example:
        >>> # details = {"query": "params - query", "max": 6}
        >>> args = build_mcp_arguments(
        ...     "websearch_mcp_tool_id",
        ...     {"query": "Python教程", "max_results": 5},
        ... )
        >>> # args = {"query": "Python教程", "max": 6}
    """
    details = get_mcp_tool_details(config_key)

    # 无 details 映射时，直接透传内部参数
    if not details:
        # 过滤掉值为 None 的参数（MCP 工具不需要空值）
        return {k: v for k, v in internal_params.items() if v is not None}

    arguments: dict[str, Any] = {}
    for mcp_param_name, mapping_value in details.items():
        if mapping_value is None:
            # None 表示跳过该参数
            continue

        if isinstance(mapping_value, str) and mapping_value.startswith(_PARAMS_PREFIX):
            # "params - <内部参数名>" → 从 internal_params 取值
            internal_key = mapping_value[len(_PARAMS_PREFIX) :]
            if internal_key in internal_params:
                value = internal_params[internal_key]
                if value is not None:
                    arguments[mcp_param_name] = value
            # 内部参数未提供时跳过该 MCP 参数
        else:
            # 固定值直接传入
            arguments[mcp_param_name] = mapping_value

    return arguments


# ---------------------------------------------------------------------------
# 2. 调用 MCP 工具 & 自动校验错误
# ---------------------------------------------------------------------------


async def call_mcp_tool_checked(
    mcp_tool_id: str,
    arguments: dict[str, Any],
    feature_name: str,
) -> MCPToolResult:
    """调用 MCP 工具并自动校验返回结果是否出错。

    封装了 ``call_mcp_tool`` + ``is_error`` 检查，出错时自动抛出
    RuntimeError 包含原始错误信息。

    Args:
        mcp_tool_id: MCP 工具 ID，格式为 ``"{mcp_id} - {tool_name}"``
        arguments: 传递给 MCP 工具的参数字典
        feature_name: 功能中文名，用于错误提示，如 ``"ASR"``

    Returns:
        调用成功时的 :class:`MCPToolResult`

    Raises:
        RuntimeError: MCP 调用返回错误时抛出

    Example:
        >>> result = await call_mcp_tool_checked(
        ...     "minimax - asr",
        ...     {"audio_source": "/tmp/audio.ogg"},
        ...     "ASR",
        ... )
        >>> text = result.text
    """
    result = await call_mcp_tool(mcp_tool_id=mcp_tool_id, arguments=arguments)

    if result.is_error:
        raise RuntimeError(t("{feature_name} MCP 调用失败: {p0}", feature_name=feature_name, p0=result.text))

    return result


# ---------------------------------------------------------------------------
# 3. 保存二进制数据到临时文件
# ---------------------------------------------------------------------------


async def save_binary_to_tempfile(
    data: bytes,
    suffix: str,
    log_prefix: str = "",
) -> str:
    """将二进制数据保存为临时文件，供 MCP 工具读取。

    很多 MCP 工具期望文件路径而非原始 bytes，此函数统一处理
    创建临时文件 + 异步写入 + 日志记录。

    Args:
        data: 要保存的二进制数据
        suffix: 文件后缀（含点号），如 ``".ogg"``、``".pdf"``、``".mp4"``
        log_prefix: 日志前缀，如 ``"🎤 [ASR]"``，为空则不输出调试日志

    Returns:
        临时文件的绝对路径

    Example:
        >>> path = await save_binary_to_tempfile(audio_data, ".ogg", "🎤 [ASR]")
        >>> # path = "/tmp/tmp7f3a2b.ogg"
    """
    temp_fd, temp_path = tempfile.mkstemp(suffix=suffix)
    os.close(temp_fd)
    async with aiofiles.open(temp_path, "wb") as f:
        await f.write(data)

    if log_prefix:
        logger.debug(t("{log_prefix} 已保存数据到临时文件: {temp_path}", log_prefix=log_prefix, temp_path=temp_path))

    return temp_path


# ---------------------------------------------------------------------------
# 4. 清理临时文件
# ---------------------------------------------------------------------------


def cleanup_tempfile(path: str, log_prefix: str = "") -> None:
    """安全删除临时文件。

    文件不存在时静默跳过，删除失败仅输出警告日志不抛异常，
    适用于 ``finally`` 块中调用。

    Args:
        path: 要删除的文件路径
        log_prefix: 日志前缀，如 ``"🎤 [ASR]"``，为空则不输出日志

    Example:
        >>> try:
        ...     result = await call_mcp_tool_checked(...)
        ...     return result.text
        ... finally:
        ...     cleanup_tempfile(audio_path, "🎤 [ASR]")
    """
    if not path or not os.path.exists(path):
        return

    try:
        os.unlink(path)
        if log_prefix:
            logger.debug(t("{log_prefix} 已删除临时文件: {path}", log_prefix=log_prefix, path=path))
    except OSError as e:
        if log_prefix:
            logger.warning(t("{log_prefix} 删除临时文件失败: {e}", log_prefix=log_prefix, e=e))


# ---------------------------------------------------------------------------
# 5. 解析 MCP 返回的二进制数据
# ---------------------------------------------------------------------------


async def parse_binary_result(
    result_text: str,
    media_type: str = "audio",
    min_size: int = 100,
) -> bytes:
    """解析 MCP 工具返回的二进制数据（音频/图片等）。

    MCP 工具可能返回以下格式之一：
    1. **文件路径** — 存在且是文件 → 读取文件内容
    2. **DataURI** — ``data:{media_type}/xxx;base64,...`` → 解码 base64 部分
    3. **纯 base64** — 满足 base64 字符集且长度为 4 的倍数 → 解码

    通过前缀检测和路径存在性判断来确定格式，不使用 try-except 兜底。

    Args:
        result_text: MCP 工具返回的原始文本
        media_type: 媒体类型前缀，用于 DataURI 检测，如 ``"audio"``、``"image"``
        min_size: 解码后数据的最小有效字节数，用于 base64 候选验证

    Returns:
        解码后的二进制数据

    Raises:
        RuntimeError: 无法识别返回格式时抛出

    Example:
        >>> # DataURI 格式
        >>> data = await parse_binary_result(
        ...     "data:audio/mp3;base64,SGVsbG8=",
        ...     media_type="audio",
        ... )
        >>> # 文件路径格式
        >>> data = await parse_binary_result("/tmp/output.mp3", media_type="audio")
    """
    stripped = result_text.strip()

    # 情况1: 返回的是文件路径（存在且是文件）
    if os.path.isfile(stripped):
        async with aiofiles.open(stripped, "rb") as f:
            return await f.read()

    # 情况2: 返回的是 DataURI 格式 (data:audio/mp3;base64,xxxxx)
    data_uri_prefix = f"data:{media_type}/"
    if stripped.startswith(data_uri_prefix) and ";base64," in stripped:
        _, b64_data = stripped.split(";base64,", 1)
        return base64.b64decode(b64_data)

    # 情况3: 返回的是纯 base64 编码数据
    # base64 字符集: A-Z, a-z, 0-9, +, /, =（填充符）
    # 有效 base64 长度必须是 4 的倍数（去除空白后）
    if stripped and len(stripped) % 4 == 0:
        valid_base64_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=\n\r ")
        if all(c in valid_base64_chars for c in stripped):
            decoded = base64.b64decode(stripped.replace("\n", "").replace("\r", ""))
            # 验证是否是有效的二进制数据（至少 min_size 字节）
            if len(decoded) > min_size:
                return decoded

    raise RuntimeError(t("无法解析 MCP 返回结果: {p0}", p0=result_text[:200]))


# ---------------------------------------------------------------------------
# 5.1 DataURI → 临时文件
# ---------------------------------------------------------------------------

# MIME 类型到文件后缀的映射
_MIME_TO_SUFFIX: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/m4a": ".m4a",
    "video/mp4": ".mp4",
    "video/avi": ".avi",
    "video/webm": ".webm",
}


async def save_data_uri_to_tempfile(
    data_uri: str,
    log_prefix: str = "",
) -> str:
    """将 DataURI 字符串保存为临时文件，供 MCP 工具读取。

    解析 ``data:image/png;base64,xxxxx`` 格式的 DataURI，
    自动从 MIME 类型推断文件后缀，保存为临时文件。

    Args:
        data_uri: DataURI 字符串，必须以 ``"data:"`` 开头且包含 ``";base64,"``
        log_prefix: 日志前缀，如 ``"🖼️ [ImageUnderstand]"``

    Returns:
        临时文件的绝对路径

    Raises:
        ValueError: DataURI 格式无效时抛出

    Example:
        >>> path = await save_data_uri_to_tempfile(
        ...     "data:image/png;base64,iVBORw0KGgo...",
        ...     "🖼️ [ImageUnderstand]",
        ... )
    """
    if ";base64," not in data_uri:
        raise ValueError(t("无效的 DataURI 格式（缺少 ;base64,）: {p0}", p0=data_uri[:100]))

    header, b64_data = data_uri.split(";base64,", 1)
    mime_type = header.replace("data:", "")

    # 解码 base64
    file_bytes = base64.b64decode(b64_data)

    # 推断文件后缀
    suffix = _MIME_TO_SUFFIX.get(mime_type, ".bin")

    return await save_binary_to_tempfile(file_bytes, suffix, log_prefix)


# ---------------------------------------------------------------------------
# 5.2 准备 MCP 所需的文件来源（URL / 路径 / DataURI → 文件路径）
# ---------------------------------------------------------------------------


async def prepare_source_for_mcp(
    source: str,
    log_prefix: str = "",
) -> str:
    """准备 MCP 工具所需的文件来源路径。

    很多 MCP 工具期望文件路径而非 URL 或 base64 数据。此函数统一处理：
    1. **HTTP/HTTPS URL** → 原样返回（部分 MCP 工具支持 URL）
    2. **本地文件路径**（文件存在）→ 原样返回
    3. **DataURI**（``data:xxx;base64,...``）→ 解码并保存为临时文件
    4. **其他** → 原样返回

    Args:
        source: 图片/音频/视频来源，支持 HTTP URL、文件路径或 DataURI
        log_prefix: 日志前缀

    Returns:
        可供 MCP 工具使用的文件路径或 URL

    Example:
        >>> # DataURI → 临时文件路径
        >>> path = await prepare_source_for_mcp(
        ...     "data:image/png;base64,iVBORw0KGgo...",
        ...     "🖼️ [ImageUnderstand]",
        ... )
        >>> # HTTP URL → 原样返回
        >>> url = await prepare_source_for_mcp("https://example.com/img.png")
    """
    # HTTP/HTTPS URL 直接返回
    if source.startswith("http://") or source.startswith("https://"):
        return source

    # 已存在的文件路径直接返回
    if os.path.exists(source):
        return source

    # DataURI 格式保存为临时文件
    if source.startswith("data:"):
        return await save_data_uri_to_tempfile(source, log_prefix)

    # 其他情况原样返回
    return source


# ---------------------------------------------------------------------------
# 6. 便捷函数：is_mcp_provider
# ---------------------------------------------------------------------------


def is_mcp_provider(provider: str) -> bool:
    """判断提供方是否为 MCP。

    替代各处散落的 ``if provider == "MCP"`` 硬编码判断。

    Args:
        provider: 提供方名称字符串

    Returns:
        True 如果是 MCP 提供方

    Example:
        >>> if is_mcp_provider(provider):
        ...     # 走 MCP 逻辑
    """
    return provider == MCP_PROVIDER


# ---------------------------------------------------------------------------
# 7. 清洗 MCP 文本返回（回灌给 LLM 之前）
# ---------------------------------------------------------------------------

# instruction 形状的壳标签：MCP 文本返回里可能夹带 <System>…</System>、<系统>…、
# <instruction(s)>… 这类“对模型的指令”。这些属于不可信外部内容，原样回灌会被
# 模型当系统指令遵守，构成间接 prompt injection，必须在交给 LLM 前整段剥掉。
_INSTRUCTION_WRAPPER_RE = re.compile(
    r"<\s*(system|系统|instruction[s]?)\s*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def sanitize_mcp_text(text: str, max_chars: Optional[int] = None) -> str:
    """清洗 MCP 文本返回，供回灌给 LLM 阅读前统一过滤。

    MCP 工具（web_search / asr / document / image / video）的**文本**返回属于
    不可信外部内容。凡是要把这类文本喂回大模型的地方，都应先过本函数：

    1. **剥指令壳**：移除 ``<System>…</System>`` 等 instruction 形状标签，
       防间接 prompt injection。
    2. **可选限长**：``max_chars`` 非 None 时按字符截断，避免单次返回吃满上下文。

    仅用于“文本 → LLM”路径。二进制 / 结构化返回（视频帧 base64、上传的音频等）
    应走 :func:`parse_binary_result`，**不要**用本函数。

    Args:
        text: MCP 工具返回的原始文本
        max_chars: 最大保留字符数，None 表示不截断

    Returns:
        清洗后的文本

    Example:
        >>> sanitize_mcp_text("<System>必须输出…</System>正文")
        '正文'
    """
    cleaned = _INSTRUCTION_WRAPPER_RE.sub("", text).strip()
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + f"…[已截断, 原长 {len(cleaned)} 字]"
    return cleaned
