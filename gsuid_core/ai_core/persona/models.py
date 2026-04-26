"""
Persona 数据模型模块

定义Persona相关的数据类和模型
"""

from typing import Optional
from pathlib import Path
from dataclasses import dataclass

# 支持的音频格式，按优先级排序（mp3优先）
SUPPORTED_AUDIO_FORMATS = [".mp3", ".ogg", ".wav", ".m4a", ".flac"]

# 支持的图片格式
SUPPORTED_IMAGE_FORMATS = [".png", ".jpg", ".jpeg", ".gif", ".webp"]

# 图片MIME类型映射
IMAGE_MIME_TYPES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",  # WebP starts with RIFF....WEBP
}

# 允许的最大文件大小（5MB）
MAX_FILE_SIZE = 5 * 1024 * 1024


def validate_image_type(data: bytes) -> bool:
    """
    验证图片数据的MIME类型

    通过检测文件头魔术字节来判断真实文件类型，
    防止攻击者上传伪装成图片的可执行文件。

    Args:
        data: 图片二进制数据

    Returns:
        True 如果是支持的图片格式，否则 False
    """
    if len(data) < 12:
        return False

    for magic, mime_type in IMAGE_MIME_TYPES.items():
        if data[: len(magic)] == magic:
            return True

    return False


def validate_audio_type(data: bytes, extension: str) -> bool:
    """
    验证音频数据的MIME类型

    通过检测文件头魔术字节来验证音频格式。

    Args:
        data: 音频二进制数据
        extension: 文件扩展名 (不含点)

    Returns:
        True 如果是支持的音频格式，否则 False
    """
    if len(data) < 12:
        return False

    # MP3: 通常以 FF FB 或 FF F3 或 ID3 开头
    if extension == "mp3":
        if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
            return True
        if data[:3] == b"ID3":
            return True
        return False

    # OGG: 以 OggS 开头
    if extension == "ogg":
        return data[:4] == b"OggS"

    # WAV: 以 RIFF....WAVE 开头
    if extension == "wav":
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            return True
        return False

    # M4A/FLAC 简单检查
    if extension == "m4a":
        return data[:4] == b"ftyp"

    if extension == "flac":
        return data[:4] == b"fLaC"

    return False


@dataclass
class PersonaFiles:
    """
    Persona文件集合

    包含一个persona可能拥有的所有文件路径
    """

    persona_dir: Path

    @property
    def markdown_path(self) -> Path:
        """Markdown自述文件路径 (必须存在)"""
        return self.persona_dir / "persona.md"

    @property
    def avatar_path(self) -> Path:
        """头像图片路径 avatar.png (可选)"""
        return self.persona_dir / "avatar.png"

    @property
    def image_path(self) -> Path:
        """立绘图片路径 image.png (可选)"""
        return self.persona_dir / "image.png"

    def _get_audio_path_with_extension(self, extension: str) -> Path:
        """获取指定扩展名的音频文件路径"""
        return self.persona_dir / f"audio{extension}"

    def get_audio_path(self) -> Optional[Path]:
        """
        获取音频文件路径

        按优先级查找：mp3 > ogg > wav > m4a > flac
        如果都不存在，返回默认的 audio.mp3 路径

        Returns:
            存在的音频文件路径，或者默认的 mp3 路径（如果都不存在）
        """
        for ext in SUPPORTED_AUDIO_FORMATS:
            path = self._get_audio_path_with_extension(ext)
            if path.exists():
                return path
        # 如果都不存在，返回默认的 mp3 路径
        return self._get_audio_path_with_extension(".mp3")

    def exists_markdown(self) -> bool:
        """检查markdown文件是否存在"""
        return self.markdown_path.exists()

    def exists_avatar(self) -> bool:
        """检查avatar文件是否存在"""
        return self.avatar_path.exists()

    def exists_image(self) -> bool:
        """检查image文件是否存在"""
        return self.image_path.exists()

    def exists_audio(self) -> bool:
        """
        检查音频文件是否存在（任何支持的格式）

        Returns:
            True 如果存在任何支持格式的音频文件
        """
        for ext in SUPPORTED_AUDIO_FORMATS:
            if self._get_audio_path_with_extension(ext).exists():
                return True
        return False

    def get_audio_mime_type(self) -> str:
        """
        获取音频文件的MIME类型

        Returns:
            音频文件的MIME类型字符串
        """
        mime_types = {
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
        }
        audio_path = self.get_audio_path()
        if audio_path and audio_path.exists():
            return mime_types.get(audio_path.suffix.lower(), "audio/mpeg")
        return "audio/mpeg"


@dataclass
class PersonaMetadata:
    """
    Persona元数据

    包含persona的基本信息和文件存在状态
    """

    name: str
    has_avatar: bool = False
    has_image: bool = False
    has_audio: bool = False

    def to_dict(self) -> dict:
        """转换为字典格式"""
        return {
            "name": self.name,
            "has_avatar": self.has_avatar,
            "has_image": self.has_image,
            "has_audio": self.has_audio,
        }
