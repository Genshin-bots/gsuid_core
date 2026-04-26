"""
History Manager APIs
提供历史会话管理相关的 RESTful APIs
包括列出所有session、查看session历史记录、清空session历史、查看session的persona等功能
"""

from typing import Dict

from fastapi import Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.history.manager import (
    history_to_prompt,
    get_history_manager,
)


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
        # 从 HistoryManager 获取所有 session 信息（已统一）
        all_sessions = manager.get_all_sessions_info()

        # 构建最终结果
        result = []
        for session_id, session_data in all_sessions.items():
            # 从 session_data 直接获取 user_id、group_id 和 bot_id
            user_id = session_data.get("user_id")
            group_id = session_data.get("group_id")
            bot_id = session_data.get("bot_id", "")

            # 判断 session 类型
            if group_id:
                session_type = "group"
            else:
                session_type = "private"

            # 从 history_manager 获取消息数量
            from gsuid_core.models import Event

            ev = Event(
                bot_id=bot_id,
                user_id=user_id or "",
                group_id=group_id,
                user_type="group" if group_id else "direct",
            )
            msg_count = manager.get_history_count(ev)

            # 检查是否有 AI session
            has_ai_session = manager.has_ai_session(session_id)
            ai_history_length = 0
            if has_ai_session:
                ai_session = manager.get_ai_session(session_id)
                if ai_session and hasattr(ai_session, "history"):
                    ai_history_length = len(ai_session.history)

            result.append(
                {
                    "session_id": session_id,
                    "type": session_type,
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

    Args:
        session_id: Session标识符，格式为 "{bot_id}%%%{group_id}%%%{user_id}"
        format_type: 返回格式，可选 "text"(文本格式)、"json"(原始JSON)、"messages"(OpenAI格式)

    Returns:
        status: 0成功，1失败
        data: 历史记录内容
    """
    try:
        # 解析session_id
        # 新格式: bot:{bot_id}:group:{group_id} 或 bot:{bot_id}:private:{user_id}
        # 旧格式兼容: {bot_id}%%%{group_id}%%%{user_id} 或 group:{group_id} / private:{user_id}
        bot_id = ""
        if session_id.startswith("bot:"):
            parts = session_id.split(":", 3)
            if len(parts) >= 4:
                bot_id = parts[1]
                if parts[2] == "group":
                    group_id = parts[3]
                    user_id = ""
                elif parts[2] == "private":
                    group_id = None
                    user_id = parts[3]
                else:
                    return {"status": 1, "msg": "无效的session_id格式", "data": None}
            else:
                return {"status": 1, "msg": "无效的session_id格式", "data": None}
        elif session_id.startswith("group:"):
            group_id = session_id[6:]
            user_id = ""
        elif session_id.startswith("private:"):
            group_id = None
            user_id = session_id[8:]
        else:
            return {
                "status": 1,
                "msg": "无效的session_id格式，应为 'bot:{bot_id}:group:{group_id}' 或 'bot:{bot_id}:private:{user_id}'",
                "data": None,
            }

        manager = get_history_manager()

        # 构造 Event 对象
        from gsuid_core.models import Event

        ev = Event(
            bot_id=bot_id,
            user_id=user_id,
            group_id=group_id,
            user_type="group" if group_id else "direct",
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
            # OpenAI messages格式
            from gsuid_core.ai_core.history.manager import history_to_messages

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
            # 默认text格式
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

    Args:
        session_id: Session标识符，格式为 "{bot_id}%%%{group_id}%%%{user_id}"
        delete_session: 是否完全删除session（释放内存），默认为False仅清空历史

    Returns:
        status: 0成功，1失败
    """
    try:
        # 解析session_id
        # 新格式: bot:{bot_id}:group:{group_id} 或 bot:{bot_id}:private:{user_id}
        # 旧格式兼容: {bot_id}%%%{group_id}%%%{user_id} 或 group:{group_id} / private:{user_id}
        bot_id = ""
        if session_id.startswith("bot:"):
            parts = session_id.split(":", 3)
            if len(parts) >= 4:
                bot_id = parts[1]
                if parts[2] == "group":
                    group_id = parts[3]
                    user_id = ""
                elif parts[2] == "private":
                    group_id = None
                    user_id = parts[3]
                else:
                    return {"status": 1, "msg": "无效的session_id格式", "data": None}
            else:
                return {"status": 1, "msg": "无效的session_id格式", "data": None}
        elif session_id.startswith("group:"):
            group_id = session_id[6:]
            user_id = ""
        elif session_id.startswith("private:"):
            group_id = None
            user_id = session_id[8:]
        else:
            return {
                "status": 1,
                "msg": "无效的session_id格式，应为 'bot:{bot_id}:group:{group_id}' 或 'bot:{bot_id}:private:{user_id}'",
                "data": None,
            }

        manager = get_history_manager()

        # 构造 Event 对象
        from gsuid_core.models import Event

        ev = Event(
            bot_id=bot_id,
            user_id=user_id,
            group_id=group_id,
            user_type="group" if group_id else "direct",
        )

        if delete_session:
            # 完全删除session（包括历史、AI session等）
            success = manager.delete_session(ev)

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
            # 仅清空历史记录（保留AI session对象但清空其history）
            success = manager.clear_history(ev)

            # 清空AI session的history（如果有）
            if manager.has_ai_session(session_id):
                session = manager.get_ai_session(session_id)
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

    Args:
        session_id: Session标识符，格式为 "{bot_id}%%%{group_id}%%%{user_id}"

    Returns:
        status: 0成功，1失败
        data: persona内容（system_prompt）
    """
    try:
        manager = get_history_manager()
        if not manager.has_ai_session(session_id):
            return {
                "status": 1,
                "msg": f"Session {session_id} 不存在或尚未创建",
                "data": None,
            }

        session = manager.get_ai_session(session_id)
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

    Returns:
        status: 0成功，1失败
        data: 统计信息
    """
    try:
        manager = get_history_manager()
        stats = manager.get_stats()

        # 同时获取ai_router中的session统计
        ai_sessions = manager.get_all_ai_sessions()

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "history_manager": stats,
                "ai_router_sessions": {
                    "count": len(ai_sessions),
                    "sessions": list(ai_sessions.keys()),
                },
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取统计信息失败: {str(e)}",
            "data": None,
        }
