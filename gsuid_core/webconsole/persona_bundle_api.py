"""人格整包的导入与导出。"""

import io

from fastapi import File, Depends, UploadFile
from fastapi.responses import StreamingResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.persona.bundle import BundleError, write_zip_to_dir, import_bundle_dir, export_persona_bundle
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import PERSONA


@app.post("/api/persona/{persona_name}/bundle/export", summary="导出人格整包", tags=PERSONA)
async def export_persona_bundle_api(
    persona_name: str,
    _user: SessionRecord = Depends(require_auth),
) -> StreamingResponse:
    try:
        payload = await export_persona_bundle(persona_name)
    except BundleError as exc:
        return StreamingResponse(
            io.BytesIO(exc.message.encode("utf-8")),
            media_type="text/plain",
            status_code=400,
        )
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{persona_name}.persona.zip"'},
    )


@app.post("/api/persona/bundle/import", summary="导入人格整包", tags=PERSONA)
async def import_persona_bundle_api(
    file: UploadFile = File(...),
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, object]:
    import tempfile
    from pathlib import Path

    data = await file.read()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        try:
            write_zip_to_dir(data, root)
            result = await import_bundle_dir(root)
        except (BundleError, OSError, ValueError) as exc:
            message = exc.message if isinstance(exc, BundleError) else "人格整包无法导入"
            return {"status": 1, "msg": message, "data": None}
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "persona_name": result.persona_name,
            "created": result.created,
            "pack_ids": result.pack_ids,
            "imported_count": len(result.imported_ids),
            "linked_count": len(result.linked_ids),
        },
    }
