"""
History Manager APIs
提供历史会话管理相关的 RESTful APIs
包括列出所有session、查看session历史记录、清空session历史、查看session的persona等功能

消息历史模块（gsuid_core.message_history）已从 ai_core 解耦为通用模块，
因此本 API 在 AI 总开关关闭（enable_ai=False）时依然可以正常读取/管理消息历史，
仅会跳过 AI 会话相关的增强信息（如 AI 会话对象、persona 等）。
"""

from typing import Dict, List, Tuple, Optional

from fastapi import File, Form, Depends, UploadFile

from gsuid_core.message_history import get_history_manager
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


def _is_ai_enabled() -> bool:
    """检测 AI 总开关是否开启。

    AI 会话对象（GsCoreAIAgent）由 ai_core 的 AISessionRegistry 管理，
    仅在 AI 开启时存在。AI 关闭时本 API 退化为纯消息历史读取/管理。
    """
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("enable").data)


def _parse_session_id(
    session_id: str,
) -> Optional[Tuple[str, str, str, Optional[str], str]]:
    """解析 session_id，返回 (WS_BOT_ID, bot_id, bot_self_id, group_id, user_id)。

    格式：{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id} 或
    {WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}。
    群聊时 user_id 为空字符串、group_id 为群号；私聊时 group_id 为 None。
    格式非法时返回 None。
    """
    parts = session_id.split(":", 4)
    if len(parts) != 5:
        return None

    ws_bot_id, bot_id, bot_self_id, target_type, target_id = parts
    if not ws_bot_id or not bot_id or not bot_self_id or not target_id:
        return None
    if target_type == "group":
        return ws_bot_id, bot_id, bot_self_id, target_id, ""
    if target_type == "private":
        return ws_bot_id, bot_id, bot_self_id, None, target_id
    return None


@app.get("/api/history/sessions")
async def list_sessions(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取所有活跃的session列表

    Returns:
        status: 0成功，1失败
        data: session列表，包含session_id、类型、消息数量等信息
    """
    try:
        manager = get_history_manager()

        # AI 关闭时不加载 AI 会话注册表，has_ai_session 恒为 False
        registry = None
        if _is_ai_enabled():
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            registry = get_ai_session_registry()

        # 从 HistoryManager 获取所有 session 信息（已统一）
        all_sessions = manager.get_all_sessions_info()

        # 构建最终结果
        result = []
        for session_id, session_data in all_sessions.items():
            # 从 session_data 直接获取 user_id、group_id、bot_id、bot_self_id 和 WS_BOT_ID
            user_id = session_data.get("user_id")
            group_id = session_data.get("group_id")
            bot_id = session_data.get("bot_id", "")
            bot_self_id = session_data.get("bot_self_id", "")
            ws_bot_id = session_data.get("WS_BOT_ID")

            # 判断 session 类型
            if group_id:
                session_type = "group"
            else:
                session_type = "private"

            # 从 history_manager 获取消息数量
            from gsuid_core.models import Event

            ev = Event(
                bot_id=bot_id,
                bot_self_id=bot_self_id,
                user_id="" if group_id else (user_id or ""),
                group_id=group_id,
                user_type="group" if group_id else "direct",
                WS_BOT_ID=ws_bot_id,
            )
            msg_count = manager.get_history_count(ev)

            # 检查是否有 AI session（AI 关闭时恒为无）
            has_ai_session = False
            ai_history_length = 0
            if registry is not None:
                has_ai_session = registry.has_ai_session(session_id)
                if has_ai_session:
                    ai_session = registry.get_ai_session(session_id)
                    if ai_session and hasattr(ai_session, "history"):
                        ai_history_length = len(ai_session.history)

            result.append(
                {
                    "session_id": session_id,
                    "type": session_type,
                    "WS_BOT_ID": ws_bot_id,
                    "bot_id": bot_id,
                    "bot_self_id": bot_self_id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_count": msg_count,
                    "last_access": session_data.get("last_access"),
                    "created_at": session_data.get("created_at"),
                    "history_length": session_data.get("history_length", 0),
                    "has_ai_session": has_ai_session,
                    "ai_history_length": ai_history_length,
                }
            )

        return {
            "status": 0,
            "msg": "ok",
            "data": result,
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取session列表失败: {str(e)}",
            "data": None,
        }


@app.get("/api/history/{session_id}")
async def get_session_history(
    session_id: str,
    format_type: str = "text",
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定session的历史记录

    本接口仅读取通用消息历史，与 AI 总开关无关，enable_ai=False 时同样可用。

    Args:
        session_id: Session标识符，格式为 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
            或 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
        format_type: 返回格式，可选 "text"(文本格式)、"json"(原始JSON)、"messages"(OpenAI格式)

    Returns:
        status: 0成功，1失败
        data: 历史记录内容
    """
    try:
        parsed = _parse_session_id(session_id)
        if parsed is None:
            return {
                "status": 1,
                "msg": (
                    "无效的session_id格式，应为 "
                    "'{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}' 或 "
                    "'{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}'"
                ),
                "data": None,
            }
        ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed

        manager = get_history_manager()

        # 构造 Event 对象
        from gsuid_core.models import Event

        ev = Event(
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            user_id=user_id,
            group_id=group_id,
            user_type="group" if group_id else "direct",
            WS_BOT_ID=ws_bot_id,
        )

        # 获取历史记录
        history = manager.get_history(ev)

        if not history:
            return {
                "status": 0,
                "msg": "该session没有历史记录",
                "data": {
                    "session_id": session_id,
                    "messages": [],
                    "count": 0,
                },
            }

        # 根据格式类型返回不同格式的数据
        if format_type == "json":
            # 原始JSON格式
            messages = [record.to_dict() for record in history]
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "session_id": session_id,
                    "messages": messages,
                    "count": len(messages),
                },
            }
        elif format_type == "messages":
            # OpenAI messages格式（纯文本转换，不依赖 AI 运行时）
            from gsuid_core.ai_core.history_format import history_to_messages

            messages = history_to_messages(history)
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "session_id": session_id,
                    "messages": messages,
                    "count": len(messages),
                },
            }
        else:
            # 默认text格式（纯文本转换，不依赖 AI 运行时）
            from gsuid_core.ai_core.history_format import history_to_prompt

            text_content = history_to_prompt(history)
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "session_id": session_id,
                    "content": text_content,
                    "count": len(history),
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取历史记录失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/history/{session_id}")
async def clear_session_history(
    session_id: str,
    delete_session: bool = False,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    清空指定session的历史记录

    - AI 开启时：清空消息历史，并同步清空/删除对应的 AI 会话对象。
    - AI 关闭时：仅清空消息历史（不存在 AI 会话对象）。

    Args:
        session_id: Session标识符，格式为 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
            或 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"
        delete_session: 是否完全删除session（释放内存），默认为False仅清空历史

    Returns:
        status: 0成功，1失败
    """
    try:
        parsed = _parse_session_id(session_id)
        if parsed is None:
            return {
                "status": 1,
                "msg": (
                    "无效的session_id格式，应为 "
                    "'{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}' 或 "
                    "'{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}'"
                ),
                "data": None,
            }
        ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed

        manager = get_history_manager()
        ai_enabled = _is_ai_enabled()

        # 构造 Event 对象
        from gsuid_core.models import Event

        ev = Event(
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            user_id=user_id,
            group_id=group_id,
            user_type="group" if group_id else "direct",
            WS_BOT_ID=ws_bot_id,
        )

        if delete_session:
            # 完全删除session：始终删除消息历史；AI 开启时一并删除 AI 会话对象
            history_deleted = manager.delete_session(ev)
            ai_deleted = False
            if ai_enabled:
                from gsuid_core.ai_core.session_registry import get_ai_session_registry

                ai_deleted = get_ai_session_registry().remove_ai_session(session_id)
            success = history_deleted or ai_deleted

            if success:
                return {
                    "status": 0,
                    "msg": f"Session {session_id} 已完全删除",
                    "data": {"session_id": session_id, "deleted": True},
                }
            else:
                return {
                    "status": 1,
                    "msg": f"Session {session_id} 不存在",
                    "data": None,
                }
        else:
            # 仅清空消息历史；AI 开启时同步清空其 AI 会话对象内部 history
            success = manager.clear_history(ev)

            if ai_enabled:
                from gsuid_core.ai_core.session_registry import get_ai_session_registry

                registry = get_ai_session_registry()
                if registry.has_ai_session(session_id):
                    session = registry.get_ai_session(session_id)
                    if session and hasattr(session, "history"):
                        session.history.clear()

            if success:
                return {
                    "status": 0,
                    "msg": f"Session {session_id} 的历史记录已清空",
                    "data": {"session_id": session_id, "cleared": True},
                }
            else:
                return {
                    "status": 0,
                    "msg": f"Session {session_id} 没有历史记录需要清空",
                    "data": {"session_id": session_id, "cleared": False},
                }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"清空历史记录失败: {str(e)}",
            "data": None,
        }


@app.get("/api/history/{session_id}/persona")
async def get_session_persona(
    session_id: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定session当前使用的persona内容

    persona 属于 AI 会话信息。AI 总开关关闭时不存在任何 AI 会话对象，
    此时该接口统一返回"session 不存在"。

    Args:
        session_id: Session标识符，格式为 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}"
            或 "{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}"

    Returns:
        status: 0成功，1失败
        data: persona内容（system_prompt）
    """
    try:
        # AI 关闭时不存在 AI 会话对象，session 视为不存在
        if not _is_ai_enabled():
            return {
                "status": 1,
                "msg": f"Session {session_id} 不存在或尚未创建",
                "data": None,
            }

        from gsuid_core.ai_core.session_registry import get_ai_session_registry

        registry = get_ai_session_registry()
        if not registry.has_ai_session(session_id):
            return {
                "status": 1,
                "msg": f"Session {session_id} 不存在或尚未创建",
                "data": None,
            }

        session = registry.get_ai_session(session_id)
        if not session:
            return {
                "status": 1,
                "msg": f"Session {session_id} 无法获取",
                "data": None,
            }

        # 获取system_prompt
        persona_content = None
        if hasattr(session, "system_prompt"):
            persona_content = session.system_prompt
        else:
            # 尝试从其他属性获取
            persona_content = getattr(session, "_system_prompt", None)

        if persona_content:
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "session_id": session_id,
                    "persona_content": persona_content,
                },
            }
        else:
            return {
                "status": 0,
                "msg": "该session没有设置persona",
                "data": {
                    "session_id": session_id,
                    "persona_content": None,
                },
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取persona失败: {str(e)}",
            "data": None,
        }


@app.get("/api/history/stats")
async def get_history_stats(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取历史管理器的统计信息

    history_manager 部分始终可用；ai_router_sessions 部分仅在 AI 开启时有数据，
    AI 关闭时返回空统计。

    Returns:
        status: 0成功，1失败
        data: 统计信息
    """
    try:
        manager = get_history_manager()
        stats = manager.get_stats()

        # AI 会话注册表统计（AI 关闭时为空）
        ai_router_sessions = {"count": 0, "sessions": []}
        if _is_ai_enabled():
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            ai_sessions = get_ai_session_registry().get_all_ai_sessions()
            ai_router_sessions = {
                "count": len(ai_sessions),
                "sessions": list(ai_sessions.keys()),
            }

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "history_manager": stats,
                "ai_router_sessions": ai_router_sessions,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取统计信息失败: {str(e)}",
            "data": None,
        }


@app.post("/api/history/{session_id}/send")
async def send_message_to_session(
    session_id: str,
    message: str = Form(""),
    image_urls: List[str] = Form(default=[]),
    images: List[UploadFile] = File(default=[]),
    at_sender: bool = Form(False),
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    向指定 session 发送一条消息（支持文本 / 图片 / 图文混排）

    请求类型为 `multipart/form-data`，前端无需自行做 base64 编码：

    - `message`：文本内容，可为空
    - `images`：图片文件，可上传多张，后端读取二进制后转换为图片消息段
    - `image_urls`：图片直链（仅 http/https），可多个
    - `at_sender`：是否 @ 发送对象（仅群聊场景有意义）

    根据 session_id 解析出 WS_BOT_ID / bot_id / bot_self_id / group_id / user_id，定位对应的 Bot 连接后，
    将文本与图片组装为消息段列表并调用 bot.send()。发送的消息会经由 target_send
    自动记录进该 session 的消息历史。本接口与 AI 总开关无关，enable_ai=False 时同样可用。

    Args:
        session_id: Session 标识符，格式为 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}`
            或 `{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}`
        message: 文本内容（Form 字段）
        image_urls: 图片直链列表（Form 字段，可重复）
        images: 上传的图片文件列表（File 字段，可重复）
        at_sender: 是否 @ 发送对象（Form 字段）

    Returns:
        status: 0成功，1失败
    """
    try:
        # 1. 解析 session_id 与发送目标
        parsed = _parse_session_id(session_id)
        if parsed is None:
            return {
                "status": 1,
                "msg": (
                    "无效的session_id格式，应为 "
                    "'{WS_BOT_ID}:{bot_id}:{bot_self_id}:group:{group_id}' 或 "
                    "'{WS_BOT_ID}:{bot_id}:{bot_self_id}:private:{user_id}'"
                ),
                "data": None,
            }
        ws_bot_id, bot_id, bot_self_id, group_id, user_id = parsed

        # 群聊发往 group_id，私聊发往 user_id
        is_group = group_id is not None
        target_id = group_id if is_group else user_id
        if not target_id:
            return {
                "status": 1,
                "msg": "session_id 中缺少有效的发送目标",
                "data": None,
            }

        # 2. 定位一个可用的 Bot WS 连接
        from gsuid_core.gss import gss

        if not gss.active_bot:
            return {
                "status": 1,
                "msg": "当前没有任何已连接的 Bot",
                "data": None,
            }

        _bot = gss.active_bot.get(ws_bot_id)
        if _bot is None:
            return {
                "status": 1,
                "msg": f"Bot WS '{ws_bot_id}' 当前未连接，无法发送消息",
                "data": None,
            }

        # 3. 组装消息段：文本 + 上传图片（二进制）+ 图片直链
        from gsuid_core.segment import MessageSegment

        text = message.strip()
        segments = []
        image_count = 0

        if text:
            segments.append(MessageSegment.text(text))

        for img_file in images:
            img_bytes = await img_file.read()
            if img_bytes:
                segments.append(MessageSegment.image(img_bytes))
                image_count += 1

        for url in image_urls:
            clean_url = url.strip()
            if not clean_url:
                continue
            # 仅允许 http(s) 直链：避免 MessageSegment.image 将任意字符串
            # 当作本地文件路径读取，造成服务器本地文件泄露
            if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
                return {
                    "status": 1,
                    "msg": f"image_urls 仅支持 http/https 直链: {clean_url}",
                    "data": None,
                }
            segments.append(MessageSegment.image(clean_url))
            image_count += 1

        if not segments:
            return {
                "status": 1,
                "msg": "消息内容不能为空（需提供 message 文本或 images/image_urls 图片）",
                "data": None,
            }

        # 4. 构造 Event 与高层 Bot 包装器后发送
        from gsuid_core.bot import Bot
        from gsuid_core.models import Event

        ev = Event(
            bot_id=bot_id,
            real_bot_id=bot_id,
            bot_self_id=bot_self_id,
            user_id=user_id,
            group_id=group_id,
            user_type="group" if is_group else "direct",
            WS_BOT_ID=ws_bot_id,
        )

        # 将旧的空 bot_self_id 会话合并到真实 bot_self_id 会话，避免发送后出现两个 session。
        legacy_ev = Event(
            bot_id=bot_id,
            real_bot_id=bot_id,
            bot_self_id="",
            user_id=user_id,
            group_id=group_id,
            user_type="group" if is_group else "direct",
            WS_BOT_ID=ws_bot_id,
        )
        get_history_manager().merge_session(legacy_ev, ev)

        bot = Bot(_bot, ev)
        await bot.send(segments, at_sender=at_sender)

        return {
            "status": 0,
            "msg": "消息发送成功",
            "data": {
                "session_id": session_id,
                "target_type": "group" if is_group else "private",
                "target_id": target_id,
                "text_sent": bool(text),
                "image_count": image_count,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"发送消息失败: {str(e)}",
            "data": None,
        }
