"""系统提示词组：列出、查看、新建、复制、保存。默认组拒绝写入。"""

from fastapi import Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.prompt_group.store import (
    PromptGroupError,
    BuiltinGroupError,
    save_group,
    list_groups,
    create_group,
    delete_group,
    group_detail,
)

from ._api_tags import PERSONA


def _fail(msg: str) -> dict[str, object]:
    return {"status": 1, "msg": msg, "data": None}


@app.get("/api/prompt-groups", summary="系统提示词组列表", tags=PERSONA)
async def get_prompt_groups(_: dict[str, object] = Depends(require_auth)) -> dict[str, object]:
    return {"status": 0, "msg": "ok", "data": list_groups()}


@app.get("/api/prompt-groups/{name}", summary="系统提示词组详情", tags=PERSONA)
async def get_prompt_group(name: str, _: dict[str, object] = Depends(require_auth)) -> dict[str, object]:
    try:
        return {"status": 0, "msg": "ok", "data": group_detail(name)}
    except PromptGroupError as e:
        return _fail(str(e))


@app.put("/api/prompt-groups/{name}", summary="保存系统提示词组", tags=PERSONA)
async def put_prompt_group(
    name: str,
    data: dict[str, object],
    _: dict[str, object] = Depends(require_auth),
) -> dict[str, object]:
    prompts = data["prompts"] if "prompts" in data else None
    if not isinstance(prompts, dict):
        return _fail("prompts 必须是对象")
    cleaned: dict[str, str] = {}
    for key, value in prompts.items():
        if not isinstance(key, str) or not isinstance(value, str):
            return _fail("prompts 的键和值都必须是字符串")
        cleaned[key] = value
    try:
        return {"status": 0, "msg": "ok", "data": save_group(name, cleaned)}
    except BuiltinGroupError as e:
        return _fail(str(e))
    except PromptGroupError as e:
        return _fail(str(e))


@app.post("/api/prompt-groups", summary="新建系统提示词组", tags=PERSONA)
async def post_prompt_group(
    data: dict[str, object],
    _: dict[str, object] = Depends(require_auth),
) -> dict[str, object]:
    name = data["name"] if "name" in data else ""
    copy_from = data["copy_from"] if "copy_from" in data else None
    if not isinstance(name, str) or not name.strip():
        return _fail("请提供名称")
    if copy_from is not None and not isinstance(copy_from, str):
        return _fail("copy_from 必须是字符串")
    source = copy_from.strip() if isinstance(copy_from, str) and copy_from.strip() else None
    try:
        return {"status": 0, "msg": "ok", "data": create_group(name.strip(), copy_from=source)}
    except BuiltinGroupError as e:
        return _fail(str(e))
    except PromptGroupError as e:
        return _fail(str(e))


@app.post("/api/prompt-groups/{name}/copy", summary="复制系统提示词组", tags=PERSONA)
async def copy_prompt_group(
    name: str,
    data: dict[str, object],
    _: dict[str, object] = Depends(require_auth),
) -> dict[str, object]:
    new_name = data["name"] if "name" in data else ""
    if not isinstance(new_name, str) or not new_name.strip():
        return _fail("请提供新名称")
    try:
        return {"status": 0, "msg": "ok", "data": create_group(new_name.strip(), copy_from=name)}
    except BuiltinGroupError as e:
        return _fail(str(e))
    except PromptGroupError as e:
        return _fail(str(e))


@app.delete("/api/prompt-groups/{name}", summary="删除系统提示词组", tags=PERSONA)
async def remove_prompt_group(name: str, _: dict[str, object] = Depends(require_auth)) -> dict[str, object]:
    try:
        delete_group(name)
    except BuiltinGroupError as e:
        return _fail(str(e))
    except PromptGroupError as e:
        return _fail(str(e))
    return {"status": 0, "msg": "ok", "data": None}
