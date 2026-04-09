"""
Persona 核心类模块

提供Persona类用于抽象和管理单个角色的人格资源
"""

from typing import Optional
from pathlib import Path

import aiofiles

from .models import PersonaFiles, PersonaMetadata
from .prompts import assistant_prompt
from ..resource import PERSONA_PATH


class Persona:
    """
    Persona 角色类

    抽象和管理单个角色的人格资源，包括：
    - Markdown自述文件 (必须存在)
    - 头像图片 avatar.png (可选)
    - 立绘图片 image.png (可选)
    - 音频文件 audio.mp3 (可选)

    每个persona在data/ai_core/persona下有自己的独立文件夹
    """

    def __init__(self, name: str):
        """
        初始化Persona实例

        Args:
            name: 角色名称，也是文件夹名称
        """
        self.name = name
        self._files = PersonaFiles(persona_dir=PERSONA_PATH / name)

    @property
    def files(self) -> PersonaFiles:
        """获取文件集合"""
        return self._files

    @property
    def dir_path(self) -> Path:
        """获取persona文件夹路径"""
        return self._files.persona_dir

    def exists(self) -> bool:
        """
        检查persona是否存在（通过检查markdown文件）

        Returns:
            True如果markdown文件存在，否则False
        """
        return self._files.exists_markdown()

    async def load_content(self) -> str:
        """
        加载persona的markdown内容

        Returns:
            markdown内容字符串

        Raises:
            FileNotFoundError: 如果markdown文件不存在
        """
        if self.name == "智能助手":
            return assistant_prompt

        if not self.exists():
            raise FileNotFoundError(f"Persona '{self.name}' 不存在")

        async with aiofiles.open(str(self._files.markdown_path), "r", encoding="utf-8") as f:
            return await f.read()

    async def save_content(self, content: str) -> None:
        """
        保存persona的markdown内容

        Args:
            content: markdown内容字符串
        """
        # 确保目录存在
        self.dir_path.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(str(self._files.markdown_path), "w", encoding="utf-8") as f:
            await f.write(content)

    def get_avatar_path(self) -> Optional[str]:
        """
        获取头像图片路径

        Returns:
            头像图片的绝对路径字符串，如果不存在则返回None
        """
        if self._files.exists_avatar():
            return str(self._files.avatar_path.absolute())
        return None

    def get_image_path(self) -> Optional[str]:
        """
        获取立绘图片路径

        Returns:
            立绘图片的绝对路径字符串，如果不存在则返回None
        """
        if self._files.exists_image():
            return str(self._files.image_path.absolute())
        return None

    def get_audio_path(self) -> Optional[str]:
        """
        获取音频文件路径

        按优先级查找：mp3 > ogg > wav > m4a > flac

        Returns:
            音频文件的绝对路径字符串，如果不存在则返回None
        """
        audio_path = self._files.get_audio_path()
        if audio_path and audio_path.exists():
            return str(audio_path.absolute())
        return None

    async def save_avatar(self, image_data: bytes) -> str:
        """
        保存头像图片

        Args:
            image_data: 图片二进制数据

        Returns:
            保存后的文件路径
        """
        self.dir_path.mkdir(parents=True, exist_ok=True)

        with open(self._files.avatar_path, "wb") as f:
            f.write(image_data)

        return str(self._files.avatar_path.absolute())

    async def save_image(self, image_data: bytes) -> str:
        """
        保存立绘图片

        Args:
            image_data: 图片二进制数据

        Returns:
            保存后的文件路径
        """
        self.dir_path.mkdir(parents=True, exist_ok=True)

        with open(self._files.image_path, "wb") as f:
            f.write(image_data)

        return str(self._files.image_path.absolute())

    async def save_audio(self, audio_data: bytes, extension: str = ".mp3") -> str:
        """
        保存音频文件

        Args:
            audio_data: 音频二进制数据
            extension: 文件扩展名，默认为 .mp3

        Returns:
            保存后的文件路径
        """
        self.dir_path.mkdir(parents=True, exist_ok=True)

        # 确保扩展名以点开头
        if not extension.startswith("."):
            extension = f".{extension}"

        audio_path = self._files.persona_dir / f"audio{extension}"
        with open(audio_path, "wb") as f:
            f.write(audio_data)

        return str(audio_path.absolute())

    def delete(self) -> bool:
        """
        删除persona及其所有文件

        Returns:
            True如果成功删除，False如果persona不存在
        """
        if not self.dir_path.exists():
            return False

        # 删除整个文件夹及其内容
        import shutil

        shutil.rmtree(self.dir_path)
        return True

    def get_metadata(self) -> PersonaMetadata:
        """
        获取persona元数据

        Returns:
            PersonaMetadata对象
        """
        return PersonaMetadata(
            name=self.name,
            has_avatar=self._files.exists_avatar(),
            has_image=self._files.exists_image(),
            has_audio=self._files.exists_audio(),
        )

    @classmethod
    def list_all(cls) -> list["Persona"]:
        """
        列出所有可用的persona

        Returns:
            Persona实例列表
        """
        personas = []
        if not PERSONA_PATH.exists():
            return personas

        for item in PERSONA_PATH.iterdir():
            if item.is_dir():
                persona = cls(item.name)
                if persona.exists():
                    personas.append(persona)

        return personas

    @classmethod
    def list_all_names(cls) -> list[str]:
        """
        列出所有可用的persona名称

        Returns:
            persona名称列表
        """
        return [p.name for p in cls.list_all()]

    @classmethod
    def get(cls, name: str) -> "Persona":
        """
        获取指定名称的Persona实例

        Args:
            name: persona名称

        Returns:
            Persona实例
        """
        return cls(name)
