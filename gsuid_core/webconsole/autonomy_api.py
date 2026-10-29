"""自主行动的审计查询和手动触发。"""

from typing import TypedDict

from fastapi import Depends

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import SessionRecord, require_auth, require_admin
from gsuid_core.webconsole._api_tags import AUTONOMY as _TAG
from gsuid_core.ai_core.autonomy.codec import FactView, StepView, load_fact, load_steps, load_str_list
from gsuid_core.ai_core.database.models import AIAutonomyActivity


class ActivityView(TypedDict):
    id: int
    persona_name: str
    bot_id: str
    bot_self_id: str
    drive_kind: str
    target_user_id: str
    target_session_key: str
    plugins_used: list[str]
    steps: list[StepView]
    fact_pack: FactView
    summary: str
    open_trails: list[str]
    delivery_mode: str
    step_count: int
    duration_ms: int
    session_id: str
    created_at: float


class ActivityListData(TypedDict):
    items: list[ActivityView]


class AutonomyFeedData(TypedDict):
    items: list[ActivityView]
    total: int
    latest: list[ActivityView]
    personas: list[str]


class TriggerData(TypedDict):
    result: str


@app.get("/api/autonomy/feed", summary="自主行动最近记录", tags=_TAG)
async def list_autonomy_feed(
    persona_name: str = "",
    limit: int = 30,
    offset: int = 0,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, int | str | AutonomyFeedData | None]:
    items, total, latest, personas = await AIAutonomyActivity.read_feed(
        persona_name=persona_name,
        limit=limit,
        offset=offset,
    )
    data: AutonomyFeedData = {
        "items": [_view(row) for row in items],
        "total": total,
        "latest": [_view(row) for row in latest],
        "personas": personas,
    }
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/autonomy/{persona_name}/activities", summary="自主行动审计", tags=_TAG)
async def list_autonomy_activities(
    persona_name: str,
    limit: int = 20,
    _user: SessionRecord = Depends(require_auth),
) -> dict[str, int | str | ActivityListData | None]:
    rows = await AIAutonomyActivity.list_recent_by_persona(persona_name, limit=limit)
    items = [_view(row) for row in rows]
    return {"status": 0, "msg": "ok", "data": {"items": items}}


@app.post("/api/autonomy/{persona_name}/trigger_once", summary="手动触发一次自主行动", tags=_TAG)
async def trigger_autonomy_once(
    persona_name: str,
    _user: SessionRecord = Depends(require_admin),
) -> dict[str, int | str | TriggerData | None]:
    from gsuid_core.ai_core.autonomy.scheduler import autonomy_scheduler

    try:
        result = await autonomy_scheduler.trigger_once(persona_name)
    except Exception as exc:
        logger.exception(t("log.ai.autonomy_run_fail", persona_name=persona_name, e=exc))
        return {"status": 1, "msg": "自主行动失败", "data": None}
    return {"status": 0, "msg": "ok", "data": {"result": result}}


def _view(row: AIAutonomyActivity) -> ActivityView:
    row_id = row.id if isinstance(row.id, int) else 0
    return {
        "id": row_id,
        "persona_name": row.persona_name,
        "bot_id": row.bot_id,
        "bot_self_id": row.bot_self_id,
        "drive_kind": row.drive_kind,
        "target_user_id": row.target_user_id,
        "target_session_key": row.target_session_key,
        "plugins_used": load_str_list(row.plugins_used_json),
        "steps": load_steps(row.steps_json),
        "fact_pack": load_fact(row.fact_pack_json),
        "summary": row.summary,
        "open_trails": load_str_list(row.open_trails_json),
        "delivery_mode": row.delivery_mode,
        "step_count": row.step_count,
        "duration_ms": row.duration_ms,
        "session_id": row.session_id,
        "created_at": row.created_at,
    }
