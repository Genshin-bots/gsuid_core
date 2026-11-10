"""多模态消息处理模块

提供对语音、视频、文档、位置、合并转发等多媒体消息类型的完整处理能力。

模块结构:
- asr.py: 语音转文字（ASR）
- video.py: 视频关键帧提取 + 多帧理解（MCP 提供方）
- frame_extract.py: 本地 ffmpeg 抽帧（model_support 未声明 video 时的兼容路径）
- gemini_files.py: Gemini File API 上传（视频直传 Google, 以 file_uri 进 messages）
- openai_files.py: OpenAI 兼容 file 块（LiteLLM 中转视频：inline base64 / /v1/files）
- document.py: 文档内容提取管道（PDF/Word/Excel → Markdown）
"""

from gsuid_core.utils.image.image_io import (
    encode_pil_image,
    sniff_image_mime,
    unwrap_image_url,
    bytes_to_data_uri,
    normalize_inline_image,
    decode_data_or_base64_uri,
    to_provider_image_content,
    guess_image_mime_from_name,
    bytes_from_inline_image_ref,
    image_bytes_to_provider_part,
    provider_prefers_binary_image,
    adapt_messages_images_for_model,
    decode_local_or_inline_image_bytes,
)

from .asr import transcribe_audio
from .video import understand_video, extract_video_frames
from .document import extract_document_content
from .gemini_files import (
    is_gemini_file_uri,
    upload_media_for_task,
    upload_media_to_gemini,
)
from .openai_files import (
    video_delivery_mode,
    video_bytes_to_openai_content,
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
