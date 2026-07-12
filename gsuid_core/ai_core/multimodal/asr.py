"""语音转文字模块（ASR）

提供统一的语音识别接口，支持多种 ASR 服务提供商。
当前支持通过 MCP 工具进行语音识别。

使用方式:
    from gsuid_core.ai_core.multimodal.asr import transcribe_audio

    text = await transcribe_audio(audio_data=b"...", format="ogg")
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.utils import (
    get_mcp_tool_id,
    is_mcp_provider,
    cleanup_tempfile,
    sanitize_mcp_text,
    build_mcp_arguments,
    call_mcp_tool_checked,
    save_binary_to_tempfile,
)
from gsuid_core.ai_core.configs.ai_config import ai_config


def _get_asr_provider() -> str:
    """获取当前配置的 ASR 服务提供方

    Returns:
        提供方名称，如 "MCP"
    """
    return ai_config.get_config("asr_provider").data


async def transcribe_audio(
    audio_data: bytes,
    audio_format: str = "ogg",
    language: str | None = None,
) -> str:
    """统一的语音转文字接口

    根据用户配置的 asr_provider 自动选择 ASR 服务。
    将音频数据转录为文本，供 AI 处理。

    Args:
        audio_data: 音频二进制数据
        audio_format: 音频格式（ogg/mp3/wav/m4a），默认 ogg
        language: 语言代码（如 "zh"、"en"），None 表示自动检测

    Returns:
        转录后的文本

    Raises:
        RuntimeError: ASR 转录失败时抛出

    Example:
        >>> text = await transcribe_audio(audio_bytes, audio_format="ogg")
        >>> print(text)
        "你好，我想问一下..."
    """
    provider = _get_asr_provider()

    if is_mcp_provider(provider):
        mcp_tool_id = get_mcp_tool_id("asr_mcp_tool_id", "ASR")

        # 将音频数据保存为临时文件
        audio_path = await save_binary_to_tempfile(audio_data, f".{audio_format}", "🎤 [ASR]")

        arguments = build_mcp_arguments(
            "asr_mcp_tool_id",
            {"audio_source": audio_path, "language": language},
        )

        try:
            result = await call_mcp_tool_checked(mcp_tool_id, arguments, "ASR")
            return sanitize_mcp_text(result.text)
        finally:
            cleanup_tempfile(audio_path, "🎤 [ASR]")

    # 未知 provider
    logger.warning(t("🎤 [ASR] 未知的提供方 '{provider}'，仅支持 MCP", provider=provider))
    raise RuntimeError(t("ASR 不支持该提供方: {provider}", provider=provider))
