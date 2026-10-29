"""人格整包：persona.md、配置、头像、立绘、音频，以及挂载的表情包。

目录就是以后的 git 仓库。绑定行不写进 persona.md。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from dataclasses import field, dataclass

from gsuid_core.utils.path_safety import is_safe_filename
from gsuid_core.ai_core.meme.library import ext_for_mime, compute_meme_id, get_folder_path
from gsuid_core.ai_core.meme.normalize import prepare_meme_image
from gsuid_core.ai_core.persona.models import SUPPORTED_AUDIO_FORMATS, SUPPORTED_IMAGE_FORMATS
from gsuid_core.ai_core.meme.pack_model import MAX_PACKS_PER_PERSONA, PackStore
from gsuid_core.ai_core.persona.persona import Persona
from gsuid_core.ai_core.persona.resource import save_persona

BUNDLE_FORMAT = "gsuid-persona-bundle"
BUNDLE_VERSION = "1.0"
_OPTIONAL_NAMES = (
    "avatar.png",
    "image.png",
    "config.json",
    "persona.json",
    "voice_anchor.txt",
    "appearance.txt",
)
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass
class BundleImportResult:
    persona_name: str
    created: bool
    pack_ids: list[str] = field(default_factory=list)
    imported_ids: list[str] = field(default_factory=list)
    linked_ids: list[str] = field(default_factory=list)


class BundleError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _tags_for_file(pack_dir: Path, file_name: str) -> tuple[str, list[str], list[str], list[str]]:
    """返回 description, emotion_tags, scene_tags, custom_tags。不从文件名猜标签。"""
    tags_path = pack_dir / "tags.json"
    if tags_path.is_file():
        raw = json.loads(tags_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if "file" not in item or item["file"] != file_name:
                    continue
                description = (
                    item["description"] if "description" in item and isinstance(item["description"], str) else ""
                )
                return (
                    description,
                    _str_list(item, "emotion_tags"),
                    _str_list(item, "scene_tags"),
                    _str_list(item, "custom_tags"),
                )
    return "", [], [], []


def _str_list(item: dict[str, object], key: str) -> list[str]:
    if key not in item:
        return []
    raw = item[key]
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            values.append(entry)
    return values


def _pack_dirs(root: Path) -> list[Path]:
    packs = root / "packs"
    if not packs.is_dir():
        return []
    found = [path for path in packs.iterdir() if path.is_dir() and is_safe_filename(path.name)]
    found.sort(key=lambda path: path.name)
    return found


def _image_files(pack_dir: Path) -> list[Path]:
    files = [
        path
        for path in pack_dir.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_FORMATS and path.name != "tags.json"
    ]
    files.sort(key=lambda path: path.name)
    return files


async def import_bundle_dir(root: Path) -> BundleImportResult:
    """把一个人格仓库目录装进当前 Core。包名非法或超出 16 个挂载时不写文件。"""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise BundleError("缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise BundleError("manifest.json 不是对象")
    if "format" not in manifest or manifest["format"] != BUNDLE_FORMAT:
        raise BundleError("不是人格整包")
    if (
        "version" not in manifest
        or not isinstance(manifest["version"], str)
        or not manifest["version"].startswith("1.")
    ):
        raise BundleError("不支持的人格整包版本")
    if "persona_name" not in manifest or not isinstance(manifest["persona_name"], str):
        raise BundleError("缺少人格名")
    persona_name = manifest["persona_name"].strip()
    if not is_safe_filename(persona_name):
        raise BundleError("人格名无效")
    persona_md = root / "persona.md"
    if not persona_md.is_file():
        raise BundleError("缺少 persona.md")

    pack_dirs = _pack_dirs(root)
    for pack_dir in pack_dirs:
        if not (1 <= len(pack_dir.name) <= 64):
            raise BundleError("包名无效或已被占用")

    persona = Persona(persona_name)
    created = not persona.exists()
    existing_binds = [] if created else await PackStore.pack_ids_for_persona(persona_name)
    new_binds = 0
    for pack_dir in pack_dirs:
        found = await PackStore.get_by_name(pack_dir.name)
        if found is None or found.pack_id not in existing_binds:
            new_binds += 1
    if len(existing_binds) + new_binds > MAX_PACKS_PER_PERSONA:
        raise BundleError(f"一个人格最多挂载 {MAX_PACKS_PER_PERSONA} 个表情包")

    content = persona_md.read_text(encoding="utf-8")
    await save_persona(persona_name, content)
    persona = Persona(persona_name)
    for name in _OPTIONAL_NAMES:
        source = root / name
        if source.is_file():
            target = persona.dir_path / name
            target.write_bytes(source.read_bytes())
    for audio in SUPPORTED_AUDIO_FORMATS:
        source = root / f"audio{audio}"
        if source.is_file():
            (persona.dir_path / source.name).write_bytes(source.read_bytes())

    imported: list[str] = []
    linked: list[str] = []
    pack_ids: list[str] = []
    from gsuid_core.ai_core.meme.database_model import AiMemeRecord

    for pack_dir in pack_dirs:
        pack = await PackStore.ensure_named_pack(pack_dir.name)
        pack_ids.append(pack.pack_id)
        for image_path in _image_files(pack_dir):
            data = image_path.read_bytes()
            mime = _IMAGE_MIME[image_path.suffix.lower()] if image_path.suffix.lower() in _IMAGE_MIME else "image/jpeg"
            prepared = prepare_meme_image(data, mime)
            data = prepared.data
            mime = prepared.mime
            meme_id = compute_meme_id(data)
            description, emotion, scene, custom = _tags_for_file(pack_dir, image_path.name)
            if not await AiMemeRecord.exists_by_meme_id(meme_id):
                folder = get_folder_path("common")
                folder.mkdir(parents=True, exist_ok=True)
                dest_name = f"{meme_id}{ext_for_mime(mime)}"
                (folder / dest_name).write_bytes(data)
                record = AiMemeRecord(
                    meme_id=meme_id,
                    file_path=f"common/{dest_name}",
                    file_size=len(data),
                    file_mime=mime,
                    width=prepared.width,
                    height=prepared.height,
                    folder="common",
                    persona_hint="common",
                    description=description,
                    emotion_tags=emotion,
                    scene_tags=scene,
                    custom_tags=custom,
                    status="manual",
                    pending_pack_id="",
                    assign_default_pack=False,
                    source_group="persona_bundle",
                )
                await AiMemeRecord.insert_record(record)
                imported.append(meme_id)
            if await PackStore.add_member(pack.pack_id, meme_id):
                linked.append(meme_id)
        await PackStore.add_bind_if_missing(persona_name, pack.pack_id)
    return BundleImportResult(
        persona_name=persona_name,
        created=created,
        pack_ids=pack_ids,
        imported_ids=imported,
        linked_ids=linked,
    )


async def export_persona_bundle(persona_name: str) -> bytes:
    """导出当前库里的这个人格和它挂载的表情包。"""
    if not is_safe_filename(persona_name):
        raise BundleError("人格名无效")
    persona = Persona(persona_name)
    if not persona.exists():
        raise BundleError("人格不存在")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(
                {"format": BUNDLE_FORMAT, "version": BUNDLE_VERSION, "persona_name": persona_name},
                ensure_ascii=False,
                indent=2,
            ),
        )
        archive.write(persona.files.markdown_path, "persona.md")
        for name in _OPTIONAL_NAMES:
            path = persona.dir_path / name
            if path.is_file():
                archive.write(path, name)
        audio = persona.files.get_audio_path()
        if audio is not None and audio.is_file():
            archive.write(audio, audio.name)
        from gsuid_core.ai_core.meme.library import get_memes_base_path
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        base = get_memes_base_path()
        for pack_id in await PackStore.pack_ids_for_persona(persona_name):
            pack = await PackStore.get_pack(pack_id)
            if pack is None or not is_safe_filename(pack.name):
                continue
            tags: list[dict[str, object]] = []
            for meme_id in await PackStore.member_meme_ids(pack_id):
                record = await AiMemeRecord.get_by_meme_id(meme_id)
                if record is None:
                    continue
                file_path = base / record.file_path
                if not file_path.is_file():
                    continue
                arc_name = f"packs/{pack.name}/{file_path.name}"
                archive.write(file_path, arc_name)
                tags.append(
                    {
                        "file": file_path.name,
                        "description": record.description,
                        "emotion_tags": record.emotion_tags,
                        "scene_tags": record.scene_tags,
                        "custom_tags": record.custom_tags,
                    }
                )
            archive.writestr(
                f"packs/{pack.name}/tags.json",
                json.dumps(tags, ensure_ascii=False, indent=2),
            )
    return buf.getvalue()


def write_zip_to_dir(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        archive.extractall(dest)
