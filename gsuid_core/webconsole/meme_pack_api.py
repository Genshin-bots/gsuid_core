"""表情包与人格绑定的控制台接口。本模块在 meme_api 的 /{meme_id} 之前导入。"""

from fastapi import Depends
from pydantic import Field, BaseModel

from gsuid_core.utils.path_safety import is_safe_filename
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.meme.library import MemeLibrary, write_member_payloads
from gsuid_core.ai_core.meme.pack_model import MAX_PACKS_PER_PERSONA, PackStore
from gsuid_core.ai_core.persona.persona import Persona
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.ai_core.meme.database_model import AiMemeRecord

from ._api_tags import MEME, PERSONA


class PackNameBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class MemberIdsBody(BaseModel):
    meme_ids: list[str] = Field(min_length=1)


class PersonaPacksBody(BaseModel):
    pack_ids: list[str]


def _pack_name_ok(name: str) -> bool:
    return 1 <= len(name) <= 64 and is_safe_filename(name)


@app.get("/api/meme/packs", summary="列出表情包", tags=MEME)
async def list_meme_packs(_user: SessionRecord = Depends(require_auth)) -> dict[str, object]:
    rows = await PackStore.list_summaries()
    data = [
        {
            "pack_id": row.pack_id,
            "name": row.name,
            "member_count": row.member_count,
            "tagged_count": row.tagged_count,
            "bound_personas": row.bound_personas,
        }
        for row in rows
    ]
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/meme/packs", summary="创建空表情包", tags=MEME)
async def create_meme_pack(body: PackNameBody, _user: SessionRecord = Depends(require_auth)) -> dict[str, object]:
    if not _pack_name_ok(body.name):
        return {"status": 1, "msg": "包名无效或已被占用", "data": None}
    if await PackStore.get_by_name(body.name) is not None:
        return {"status": 1, "msg": "包名无效或已被占用", "data": None}
    created = await PackStore.create_pack(body.name)
    return {"status": 0, "msg": "ok", "data": {"pack_id": created.pack_id, "name": created.name}}


@app.post("/api/meme/packs/reindex_payload", summary="按成员回填向量 payload", tags=MEME)
async def reindex_pack_payload(_user: SessionRecord = Depends(require_auth)) -> dict[str, object]:
    await write_member_payloads(strict=False)
    return {"status": 0, "msg": "ok", "data": None}


@app.patch("/api/meme/packs/{pack_id}", summary="重命名表情包", tags=MEME)
async def rename_meme_pack(
    pack_id: str,
    body: PackNameBody,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    if await PackStore.get_pack(pack_id) is None:
        return {"status": 1, "msg": "表情包不存在", "data": None}
    if not _pack_name_ok(body.name):
        return {"status": 1, "msg": "包名无效或已被占用", "data": None}
    occupied = await PackStore.get_by_name(body.name)
    if occupied is not None and occupied.pack_id != pack_id:
        return {"status": 1, "msg": "包名无效或已被占用", "data": None}
    await PackStore.rename_pack(pack_id, body.name)
    return {"status": 0, "msg": "ok", "data": {"pack_id": pack_id, "name": body.name}}


@app.delete("/api/meme/packs/{pack_id}", summary="删除表情包", tags=MEME)
async def delete_meme_pack(pack_id: str, _user: SessionRecord = Depends(require_auth)) -> dict[str, object]:
    if await PackStore.get_pack(pack_id) is None:
        return {"status": 1, "msg": "表情包不存在", "data": None}
    result = await PackStore.delete_pack(pack_id)
    await write_member_payloads(result.meme_ids, strict=False)
    if result.lost_personas:
        names = "、".join(result.lost_personas)
        msg = f"已删除表情包。{names} 失去最后一条绑定，不再发表情，直到重新绑定。"
    else:
        msg = "已删除表情包。"
    return {"status": 0, "msg": msg, "data": {"lost_personas": result.lost_personas}}


@app.get("/api/meme/packs/{pack_id}/members", summary="列出表情包成员", tags=MEME)
async def list_pack_members(
    pack_id: str,
    page: int = 1,
    page_size: int = 20,
    q: str = "",
    status: str = "",
    sort: str = "created_at_desc",
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    if await PackStore.get_pack(pack_id) is None:
        return {"status": 1, "msg": "表情包不存在", "data": None}
    if q:
        found = await MemeLibrary.search_by_text(q, pack_ids=(pack_id,), top_k=max(page * page_size, page_size))
        start = (page - 1) * page_size
        page_rows = list(found)[start : start + page_size]
        total = len(found)
    else:
        page_rows, total = await AiMemeRecord.list_pack_members(
            pack_id,
            status=status or None,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "records": [_member_dict(row) for row in page_rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


def _member_dict(record: AiMemeRecord) -> dict[str, object]:
    return {
        "meme_id": record.meme_id,
        "file_path": record.file_path,
        "description": record.description,
        "emotion_tags": record.emotion_tags,
        "scene_tags": record.scene_tags,
        "custom_tags": record.custom_tags,
        "status": record.status,
        "persona_hint": record.persona_hint,
        "folder": record.folder,
        "use_count": record.use_count,
    }


@app.post("/api/meme/packs/{pack_id}/members", summary="向表情包加入成员", tags=MEME)
async def add_pack_members(
    pack_id: str,
    body: MemberIdsBody,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    if await PackStore.get_pack(pack_id) is None:
        return {"status": 1, "msg": "表情包不存在", "data": None}
    added: list[str] = []
    for meme_id in body.meme_ids:
        if await AiMemeRecord.get_by_meme_id(meme_id) is None:
            continue
        if await PackStore.add_member(pack_id, meme_id):
            added.append(meme_id)
    await write_member_payloads(body.meme_ids, strict=False)
    return {"status": 0, "msg": "ok", "data": {"added": added}}


@app.delete("/api/meme/packs/{pack_id}/members", summary="从表情包移出成员", tags=MEME)
async def remove_pack_members(
    pack_id: str,
    body: MemberIdsBody,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    if await PackStore.get_pack(pack_id) is None:
        return {"status": 1, "msg": "表情包不存在", "data": None}
    removed = await PackStore.remove_members(pack_id, body.meme_ids)
    await write_member_payloads(body.meme_ids, strict=False)
    return {"status": 0, "msg": "ok", "data": {"removed": removed}}


@app.get("/api/persona/{persona_name}/meme_packs", summary="读取人格挂载的表情包", tags=PERSONA)
async def get_persona_meme_packs(
    persona_name: str,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    if not is_safe_filename(persona_name):
        return {"status": 1, "msg": "人格名无效", "data": None}
    if not Persona(persona_name).exists():
        return {"status": 1, "msg": "人格不存在", "data": None}
    pack_ids = await PackStore.pack_ids_for_persona(persona_name)
    return {"status": 0, "msg": "ok", "data": {"pack_ids": pack_ids}}


@app.put("/api/persona/{persona_name}/meme_packs", summary="替换人格挂载的表情包", tags=PERSONA)
async def put_persona_meme_packs(
    persona_name: str,
    body: PersonaPacksBody,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    if not is_safe_filename(persona_name):
        return {"status": 1, "msg": "人格名无效", "data": None}
    if not Persona(persona_name).exists():
        return {"status": 1, "msg": "人格不存在", "data": None}
    if len(body.pack_ids) != len(set(body.pack_ids)):
        return {"status": 1, "msg": "表情包重复", "data": None}
    if len(body.pack_ids) > MAX_PACKS_PER_PERSONA:
        return {"status": 1, "msg": f"一个人格最多挂载 {MAX_PACKS_PER_PERSONA} 个表情包", "data": None}
    for pack_id in body.pack_ids:
        if await PackStore.get_pack(pack_id) is None:
            return {"status": 1, "msg": "表情包不存在", "data": None}
    await PackStore.replace_binds(persona_name, body.pack_ids)
    return {"status": 0, "msg": "ok", "data": {"pack_ids": list(body.pack_ids)}}
