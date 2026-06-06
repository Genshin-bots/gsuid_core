"""视频理解模块

提供视频关键帧提取和多帧理解能力。
从视频中提取关键帧，然后使用图片理解模块对关键帧进行分析。

使用方式:
    from gsuid_core.ai_core.multimodal.video import extract_video_frames, understand_video

    frames = await extract_video_frames(video_data=b"...", max_frames=5)
    description = await understand_video(video_data=b"...", prompt="描述视频内容")
"""

import os
import json
import base64

import aiofiles

from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.utils import (
    get_mcp_tool_id,
    is_mcp_provider,
    cleanup_tempfile,
    sanitize_mcp_text,
    build_mcp_arguments,
    call_mcp_tool_checked,
    save_binary_to_tempfile,
    get_mcp_tool_id_optional,
)
from gsuid_core.ai_core.configs.ai_config import ai_config


def _get_video_provider() -> str:
    """获取当前配置的视频理解服务提供方

    Returns:
        提供方名称，如 "MCP"
    """
    return ai_config.get_config("video_understand_provider").data


async def extract_video_frames(
    video_data: bytes,
    video_format: str = "mp4",
    max_frames: int = 5,
    interval_seconds: float | None = None,
) -> list[bytes]:
    """从视频中提取关键帧

    使用 MCP 工具从视频中提取关键帧图片。

    Args:
        video_data: 视频二进制数据
        video_format: 视频格式，默认 mp4
        max_frames: 最大提取帧数，默认 5
        interval_seconds: 提取间隔（秒），None 表示自动选择

    Returns:
        关键帧图片二进制数据列表

    Raises:
        RuntimeError: 帧提取失败时抛出
    """
    provider = _get_video_provider()

    if is_mcp_provider(provider):
        mcp_tool_id = get_mcp_tool_id("video_extract_mcp_tool_id", "Video Extract")

        video_path = await save_binary_to_tempfile(video_data, f".{video_format}", "🎬 [Video]")

        arguments = build_mcp_arguments(
            "video_extract_mcp_tool_id",
            {
                "video_source": video_path,
                "max_frames": max_frames,
                "interval_seconds": interval_seconds,
            },
        )

        try:
            result = await call_mcp_tool_checked(mcp_tool_id, arguments, "Video Extract")

            # 解析返回的帧数据（可能是 base64 列表或文件路径列表）
            return await _parse_frames_result(result.text)
        finally:
            cleanup_tempfile(video_path, "🎬 [Video]")

    logger.warning(f"🎬 [Video] 未知的提供方 '{provider}'，仅支持 MCP")
    raise RuntimeError(f"视频帧提取不支持该提供方: {provider}")


async def understand_video(
    video_data: bytes,
    video_format: str = "mp4",
    prompt: str | None = None,
    max_frames: int = 5,
) -> str:
    """统一的视频理解接口

    从视频中提取关键帧，然后使用图片理解模块对关键帧进行综合分析。

    Args:
        video_data: 视频二进制数据
        video_format: 视频格式，默认 mp4
        prompt: 对视频的提问或分析要求，默认为通用描述
        max_frames: 最大提取帧数，默认 5

    Returns:
        视频内容的文本描述

    Raises:
        RuntimeError: 视频理解失败时抛出

    Example:
        >>> description = await understand_video(video_bytes, prompt="这个视频在讲什么")
        >>> print(description)
        "视频展示了一个烹饪过程..."
    """
    if not prompt:
        prompt = "请详细描述这个视频的内容，包括主要场景、人物动作、文字信息等。"

    provider = _get_video_provider()

    if is_mcp_provider(provider):
        # 方案1: 直接使用 MCP 视频理解工具（如果有的话）。这里用 optional 版本：
        # 未配置直连工具时返回 ""，落到下方方案2（关键帧提取 + 图片理解）；若用会抛异常
        # 的 get_mcp_tool_id，未配置时会直接 raise，方案2 永远变成死代码。
        direct_tool_id = get_mcp_tool_id_optional("video_understand_mcp_tool_id")

        if direct_tool_id:
            video_path = await save_binary_to_tempfile(video_data, f".{video_format}", "🎬 [Video]")
            try:
                arguments = build_mcp_arguments(
                    "video_understand_mcp_tool_id",
                    {"video_source": video_path, "prompt": prompt},
                )
                result = await call_mcp_tool_checked(
                    direct_tool_id,
                    arguments,
                    "Video Understand",
                )
                return sanitize_mcp_text(result.text)
            finally:
                cleanup_tempfile(video_path)

        # 方案2: 提取关键帧 + 图片理解
        logger.info(f"🎬 [Video] 使用关键帧提取 + 图片理解方案，提取 {max_frames} 帧")
        frames = await extract_video_frames(video_data, video_format, max_frames)

        if not frames:
            return "[视频帧提取失败，无法分析视频内容]"

        from gsuid_core.ai_core.image_understand import understand_image

        descriptions: list[str] = []
        for idx, frame_data in enumerate(frames):
            # 将帧数据转为 base64 DataURI
            b64 = base64.b64encode(frame_data).decode("utf-8")
            image_url = f"data:image/jpeg;base64,{b64}"

            try:
                desc = await understand_image(
                    image_url=image_url,
                    prompt=f"这是视频的第 {idx + 1} 帧。{prompt}",
                )
                descriptions.append(f"帧{idx + 1}: {desc}")
            except Exception as e:
                logger.error(f"🎬 [Video] 帧 {idx + 1} 理解失败: {e}")
                descriptions.append(f"帧{idx + 1}: [理解失败]")

        combined = "\n".join(descriptions)
        return f"【视频关键帧分析】\n{combined}"

    logger.warning(f"🎬 [Video] 未知的提供方 '{provider}'，仅支持 MCP")
    raise RuntimeError(f"视频理解不支持该提供方: {provider}")


async def _parse_frames_result(result_text: str) -> list[bytes]:
    """解析帧提取 MCP 工具的返回结果

    支持两种格式：
    1. JSON 数组：每个元素是 base64 编码的图片数据（字符串或 {"data": "base64..."} 字典）
    2. 每行一个文件路径

    通过前缀检测确定格式，不使用 try-except 兜底。

    Args:
        result_text: MCP 工具返回的文本

    Returns:
        帧图片二进制数据列表

    Raises:
        RuntimeError: 无法识别返回格式时抛出
    """
    stripped = result_text.strip()

    # 情况1: JSON 数组格式
    if stripped.startswith("["):
        frames_data = json.loads(stripped)
        if isinstance(frames_data, list):
            result: list[bytes] = []
            for item in frames_data:
                if isinstance(item, str):
                    decoded = base64.b64decode(item)
                    result.append(decoded)
                elif isinstance(item, dict) and "data" in item:
                    decoded = base64.b64decode(item["data"])
                    result.append(decoded)
            if result:
                return result
        raise RuntimeError(f"JSON 数组格式但无有效帧数据: {result_text[:200]}")

    # 情况2: 每行一个文件路径
    lines = stripped.split("\n")
    result = []
    for line in lines:
        path = line.strip()
        if path and os.path.isfile(path):
            async with aiofiles.open(path, "rb") as f:
                result.append(await f.read())

    if result:
        return result

    raise RuntimeError(f"无法解析视频帧提取结果: {result_text[:200]}")
