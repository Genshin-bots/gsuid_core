"""多模态消息处理模块

提供对语音、视频、文档、位置、合并转发等多媒体消息类型的完整处理能力。

模块结构:
- asr.py: 语音转文字（ASR）
- video.py: 视频关键帧提取 + 多帧理解
- document.py: 文档内容提取管道（PDF/Word/Excel → Markdown）
"""

from .asr import transcribe_audio
from .video import understand_video, extract_video_frames
from .document import extract_document_content

__all__ = [
    "transcribe_audio",
    "extract_video_frames",
    "understand_video",
    "extract_document_content",
]
