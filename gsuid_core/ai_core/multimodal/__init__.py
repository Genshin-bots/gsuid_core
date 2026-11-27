"""多模态消息处理模块

提供对语音、视频、文档、位置、合并转发等多媒体消息类型的完整处理能力。

模块结构:
- asr.py: 语音转文字（ASR）
- video.py: 视频关键帧提取 + 多帧理解（MCP 提供方）
- frame_extract.py: 本地 ffmpeg 抽帧（model_support 未声明 video 时的兼容路径）
- gemini_files.py: Gemini File API 上传（视频直传 Google, 以 file_uri 进 messages）
- openai_files.py: OpenAI 兼容 file 块（LiteLLM 中转视频：inline base64 / /v1/files）
- document.py: 文档内容提取管道（PDF/Word/Excel → Markdown）
- payload.py: 图片/视频媒体载荷构造与物化
- inbound_prepare.py: 入站用户消息的视频/图片适配预处理
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gsuid_core.utils.image.image_io import (
        encode_pil_image as encode_pil_image,
        sniff_image_mime as sniff_image_mime,
        unwrap_image_url as unwrap_image_url,
        bytes_to_data_uri as bytes_to_data_uri,
        normalize_inline_image as normalize_inline_image,
        decode_data_or_base64_uri as decode_data_or_base64_uri,
        to_provider_image_content as to_provider_image_content,
        guess_image_mime_from_name as guess_image_mime_from_name,
        bytes_from_inline_image_ref as bytes_from_inline_image_ref,
        image_bytes_to_provider_part as image_bytes_to_provider_part,
        provider_prefers_binary_image as provider_prefers_binary_image,
        adapt_messages_images_for_model as adapt_messages_images_for_model,
        decode_local_or_inline_image_bytes as decode_local_or_inline_image_bytes,
    )

    from .asr import transcribe_audio as transcribe_audio
    from .video import (
        understand_video as understand_video,
        extract_video_frames as extract_video_frames,
    )
    from .document import extract_document_content as extract_document_content
    from .gemini_files import (
        is_gemini_file_uri as is_gemini_file_uri,
        upload_media_for_task as upload_media_for_task,
        upload_media_to_gemini as upload_media_to_gemini,
    )
    from .openai_files import (
        video_delivery_mode as video_delivery_mode,
        video_bytes_to_openai_content as video_bytes_to_openai_content,
    )
    from .frame_extract import extract_frames_ffmpeg as extract_frames_ffmpeg

_EXPORTS: dict[str, str] = {
    "transcribe_audio": "gsuid_core.ai_core.multimodal.asr",
    "extract_video_frames": "gsuid_core.ai_core.multimodal.video",
    "understand_video": "gsuid_core.ai_core.multimodal.video",
    "extract_document_content": "gsuid_core.ai_core.multimodal.document",
    "extract_frames_ffmpeg": "gsuid_core.ai_core.multimodal.frame_extract",
    "is_gemini_file_uri": "gsuid_core.ai_core.multimodal.gemini_files",
    "upload_media_to_gemini": "gsuid_core.ai_core.multimodal.gemini_files",
    "upload_media_for_task": "gsuid_core.ai_core.multimodal.gemini_files",
    "video_delivery_mode": "gsuid_core.ai_core.multimodal.openai_files",
    "video_bytes_to_openai_content": "gsuid_core.ai_core.multimodal.openai_files",
    "sniff_image_mime": "gsuid_core.utils.image.image_io",
    "guess_image_mime_from_name": "gsuid_core.utils.image.image_io",
    "unwrap_image_url": "gsuid_core.utils.image.image_io",
    "encode_pil_image": "gsuid_core.utils.image.image_io",
    "decode_data_or_base64_uri": "gsuid_core.utils.image.image_io",
    "bytes_from_inline_image_ref": "gsuid_core.utils.image.image_io",
    "decode_local_or_inline_image_bytes": "gsuid_core.utils.image.image_io",
    "normalize_inline_image": "gsuid_core.utils.image.image_io",
    "bytes_to_data_uri": "gsuid_core.utils.image.image_io",
    "provider_prefers_binary_image": "gsuid_core.utils.image.image_io",
    "image_bytes_to_provider_part": "gsuid_core.utils.image.image_io",
    "to_provider_image_content": "gsuid_core.utils.image.image_io",
    "adapt_messages_images_for_model": "gsuid_core.utils.image.image_io",
}

__all__ = [
    "transcribe_audio",
    "extract_video_frames",
    "understand_video",
    "extract_document_content",
    "extract_frames_ffmpeg",
    "is_gemini_file_uri",
    "upload_media_to_gemini",
    "upload_media_for_task",
    "video_delivery_mode",
    "video_bytes_to_openai_content",
    "sniff_image_mime",
    "guess_image_mime_from_name",
    "unwrap_image_url",
    "encode_pil_image",
    "decode_data_or_base64_uri",
    "bytes_from_inline_image_ref",
    "decode_local_or_inline_image_bytes",
    "normalize_inline_image",
    "bytes_to_data_uri",
    "provider_prefers_binary_image",
    "image_bytes_to_provider_part",
    "to_provider_image_content",
    "adapt_messages_images_for_model",
]


def __getattr__(name: str) -> object:
    mod_path = _EXPORTS[name] if name in _EXPORTS else None
    if mod_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod = importlib.import_module(mod_path)
    if name not in mod.__dict__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    val = mod.__dict__[name]
    globals()[name] = val
    return val
