"""多模态消息处理模块

提供对语音、视频、文档、位置、合并转发等多媒体消息类型的完整处理能力。

模块结构:
- asr.py: 语音转文字（ASR）
- video.py: 视频关键帧提取 + 多帧理解（MCP 提供方）
- frame_extract.py: 本地 ffmpeg 按固定间隔抽帧（非 Gemini 模型的视频兼容路径）
- gemini_files.py: Gemini File API 上传（视频直传 Google, 以 file_uri 进 messages）
- document.py: 文档内容提取管道（PDF/Word/Excel → Markdown）
"""

from .asr import transcribe_audio
from .video import understand_video, extract_video_frames
from .document import extract_document_content
from .gemini_files import (
    is_gemini_file_uri,
    upload_media_for_task,
    upload_media_to_gemini,
)
from .frame_extract import extract_frames_ffmpeg

__all__ = [
    "transcribe_audio",
    "extract_video_frames",
    "understand_video",
    "extract_document_content",
    "extract_frames_ffmpeg",
    "is_gemini_file_uri",
    "upload_media_to_gemini",
    "upload_media_for_task",
]
