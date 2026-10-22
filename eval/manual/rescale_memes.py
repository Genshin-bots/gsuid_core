"""把库里已有表情的短边收成 156 像素。保留 meme_id。

运行：uv run python eval/manual/rescale_memes.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PIL import UnidentifiedImageError

from gsuid_core.utils.path_safety import PathEscapeError, safe_join
from gsuid_core.ai_core.meme.library import ext_for_mime, get_memes_base_path
from gsuid_core.ai_core.meme.normalize import prepare_meme_image
from gsuid_core.utils.database.base_models import init_database
from gsuid_core.ai_core.meme.database_model import AiMemeRecord


async def rescale_stored_memes() -> None:
    await init_database()
    records = await AiMemeRecord.get_all_records_no_page()
    base = get_memes_base_path()
    changed = 0
    skipped = 0
    missing = 0
    failed = 0
    for record in records:
        try:
            path = safe_join(base, record.file_path)
        except PathEscapeError:
            failed += 1
            print(f"fail {record.meme_id} bad path")
            continue
        if not path.is_file():
            missing += 1
            continue
        raw = path.read_bytes()
        try:
            prepared = prepare_meme_image(raw, record.file_mime)
        except (OSError, UnidentifiedImageError, ValueError) as exc:
            failed += 1
            print(f"fail {record.meme_id} {exc}")
            continue
        ext = ext_for_mime(prepared.mime)
        new_path = path.with_name(f"{record.meme_id}{ext}")
        parent = Path(record.file_path).parent.as_posix()
        relative = f"{parent}/{new_path.name}" if parent != "." else new_path.name
        same_bytes = prepared.data == raw and new_path == path
        same_row = (
            record.width == prepared.width
            and record.height == prepared.height
            and record.file_size == len(prepared.data)
            and record.file_mime == prepared.mime
            and record.file_path.replace("\\", "/") == relative
        )
        if same_bytes and same_row:
            skipped += 1
            continue
        if not same_bytes:
            temporary = new_path.with_name(new_path.name + ".writing")
            temporary.write_bytes(prepared.data)
            temporary.replace(new_path)
            if path != new_path and path.exists():
                path.unlink()
        await AiMemeRecord.update_record(
            record.meme_id,
            {
                "file_path": relative,
                "file_size": len(prepared.data),
                "file_mime": prepared.mime,
                "width": prepared.width,
                "height": prepared.height,
            },
        )
        changed += 1
        print(f"scaled {record.meme_id} -> {prepared.width}x{prepared.height}")
    print(f"changed={changed} skipped={skipped} missing={missing} failed={failed}")


if __name__ == "__main__":
    asyncio.run(rescale_stored_memes())
