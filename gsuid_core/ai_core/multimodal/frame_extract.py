"""本地 ffmpeg 视频抽帧

给「非 Gemini 但声明支持视频分析」的模型做兼容: 视频无法直接进 messages,
按固定间隔(默认 2 秒一帧)抽成 JPEG 序列, 由调用方转成 image_url 列表提交。

与 ``multimodal/video.py`` 的 MCP 抽帧不同, 本模块直接调本机 ``ffmpeg``
可执行文件(异步子进程), 不依赖任何外部服务; 未安装 ffmpeg 时抛出明确错误。

使用方式:
    from gsuid_core.ai_core.multimodal.frame_extract import extract_frames_ffmpeg

    frames = await extract_frames_ffmpeg(video_bytes, interval_seconds=2.0)
"""

from __future__ import annotations

import os
import shutil
import asyncio
import tempfile
from pathlib import Path

import aiofiles

from gsuid_core.i18n import t
from gsuid_core.logger import logger

#: 抽帧张数上限(防止长视频转出上百帧撑爆上下文), 可用环境变量覆盖
DEFAULT_MAX_FRAMES = int(os.environ.get("GSCORE_VIDEO_FRAME_MAX", "30"))

# JPEG 质量(ffmpeg -q:v, 2~5 为高质量区间)
_JPEG_QUALITY = "3"
# 抽帧输出的最长边(过大的原始分辨率没有必要喂给模型)
_MAX_EDGE = 1024


def _find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError(t("🎬 [FrameExtract] 未找到 ffmpeg 可执行文件, 视频抽帧需要安装 ffmpeg 并加入 PATH"))
    return path


async def extract_frames_ffmpeg(
    video_data: bytes,
    video_format: str = "mp4",
    interval_seconds: float = 2.0,
    max_frames: int = 0,
) -> list[bytes]:
    """按固定时间间隔从视频中抽帧, 返回 JPEG 字节列表(按时间顺序)。

    帧数超过 ``max_frames`` 时**等距下采样**(保头保尾), 而不是截断——
    截断会让模型完全看不到视频后半段。

    Args:
        video_data: 视频二进制数据。
        video_format: 视频容器格式(临时文件扩展名), 默认 mp4。
        interval_seconds: 抽帧间隔(秒), 默认 2 秒一帧。
        max_frames: 帧数上限; <=0 用 ``DEFAULT_MAX_FRAMES``(env GSCORE_VIDEO_FRAME_MAX)。

    Raises:
        RuntimeError: ffmpeg 不存在 / 抽帧失败 / 无输出帧。
    """
    ffmpeg = _find_ffmpeg()
    limit = max_frames if max_frames > 0 else DEFAULT_MAX_FRAMES
    interval = max(0.1, float(interval_seconds))

    tmp_dir = Path(tempfile.mkdtemp(prefix="gscore_frames_"))
    video_path = tmp_dir / f"input.{video_format}"
    try:
        async with aiofiles.open(video_path, "wb") as f:
            await f.write(video_data)

        # fps=1/interval: 每 interval 秒取一帧; scale 限制最长边, 保持宽高比
        out_pattern = str(tmp_dir / "frame_%05d.jpg")
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-vf",
            f"fps=1/{interval},scale='min({_MAX_EDGE},iw)':'min({_MAX_EDGE},ih)':force_original_aspect_ratio=decrease",
            "-q:v",
            _JPEG_QUALITY,
            out_pattern,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                t(
                    "🎬 [FrameExtract] ffmpeg 抽帧失败(exit={code}): {err}",
                    code=proc.returncode,
                    err=stderr.decode(errors="replace")[-500:],
                )
            )

        frame_files = sorted(tmp_dir.glob("frame_*.jpg"))
        if not frame_files:
            raise RuntimeError(t("🎬 [FrameExtract] ffmpeg 未输出任何帧(视频可能损坏或无视频流)"))

        # 等距下采样到上限(保头保尾)
        if len(frame_files) > limit:
            step = (len(frame_files) - 1) / (limit - 1) if limit > 1 else 0
            frame_files = [frame_files[round(i * step)] for i in range(limit)]

        frames: list[bytes] = []
        for fp in frame_files:
            async with aiofiles.open(fp, "rb") as f:
                frames.append(await f.read())

        logger.info(
            t(
                "🎬 [FrameExtract] 抽帧完成: 每 {interval}s 一帧, 共 {n} 帧(上限 {limit})",
                interval=interval,
                n=len(frames),
                limit=limit,
            )
        )
        return frames
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


__all__ = ["DEFAULT_MAX_FRAMES", "extract_frames_ffmpeg"]
