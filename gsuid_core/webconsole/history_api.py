"""
History Manager APIs
提供历史会话管理相关的 RESTful APIs
包括列出所有session、查看session历史记录、清空session历史、查看session的persona等功能
"""

from typing import Dict

from fastapi import Depends

from gsuid_core.ai_core.ai_router import (
    SessionManager,
    session_history,
)
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
        # 从 ai_router 获取 session 信息（与 persona API 使用相同的 session_id 格式）
        ai_router_sessions = SessionManager.get_all_sessions_info()

        result = []
        for session_id, session_info in ai_router_sessions.items():
            # 解析 session_id 格式: "{user_id}%%%{group_id}"
            if "%%%" in session_id:
                user_id_str, group_id_str = session_id.split("%%%", 1)
                user_id = user_id_str if user_id_str != "None" else None
                group_id = group_id_str if group_id_str != "None" else None
            else:
                # 兼容旧格式（不应该发生）
                user_id = None
                group_id = None

            # 判断 session 类型
            if group_id:
                session_type = "group"
            elif user_id:
                session_type = "private"
            else:
                session_type = "unknown"

            # 从 history_manager 获取消息数量
            if group_id:
                msg_count = manager.get_history_count(group_id, user_id or "")
            elif user_id:
                msg_count = manager.get_history_count(None, user_id)
            else:
                msg_count = 0

            result.append(
                {
                    "session_id": session_id,
                    "type": session_type,
                    "group_id": group_id,
                    "user_id": user_id,
                    "message_count": msg_count,
                    "last_access": session_info.get("last_access"),
                    "created_at": session_info.get("created_at"),
                    "history_length": session_info.get("history_length", 0),
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
        session_id: Session标识符，格式为 "{user_id}%%%{group_id}"
        format_type: 返回格式，可选 "text"(文本格式)、"json"(原始JSON)、"messages"(OpenAI格式)

    Returns:
        status: 0成功，1失败
        data: 历史记录内容
    """
    try:
        # 解析session_id (使用 %%% 作为分隔符)
        if "%%%" not in session_id:
            return {
                "status": 1,
                "msg": "无效的session_id格式，应为 '{user_id}%%%{group_id}'",
                "data": None,
            }

        user_id, group_id_str = session_id.split("%%%", 1)
        group_id = group_id_str if group_id_str != "None" else None

        manager = get_history_manager()

        # 获取历史记录
        if group_id:
            # 群聊场景
            history = manager.get_history(group_id, user_id)
        else:
            # 私聊场景
            history = manager.get_history(None, user_id)

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
        session_id: Session标识符，格式为 "{user_id}%%%{group_id}"
        delete_session: 是否完全删除session（释放内存），默认为False仅清空历史

    Returns:
        status: 0成功，1失败
    """
    try:
        # 解析session_id (使用 %%% 作为分隔符)
        if "%%%" not in session_id:
            return {
                "status": 1,
                "msg": "无效的session_id格式，应为 '{user_id}%%%{group_id}'",
                "data": None,
            }

        user_id, group_id_str = session_id.split("%%%", 1)
        group_id = group_id_str if group_id_str != "None" else None

        manager = get_history_manager()

        if delete_session:
            # 完全删除session
            if group_id:
                success = manager.delete_session(group_id, user_id)
            else:
                success = manager.delete_session(None, user_id)

            # 同时清理ai_router中的session
            SessionManager.remove_session(session_id)

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
            # 仅清空历史记录
            if group_id:
                success = manager.clear_history(group_id, user_id)
            else:
                success = manager.clear_history(None, user_id)

            # 同时清理ai_router中session的历史
            if session_id in session_history:
                session = session_history[session_id]
                if hasattr(session, "history"):
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
        session_id: Session标识符，格式为 "{user_id}%%%{group_id}"

    Returns:
        status: 0成功，1失败
        data: persona内容（system_prompt）
    """
    try:
        if session_id not in session_history:
            return {
                "status": 1,
                "msg": f"Session {session_id} 不存在或尚未创建",
                "data": None,
            }

        session = session_history[session_id]

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
        ai_router_sessions = SessionManager.get_all_sessions_info()

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "history_manager": stats,
                "ai_router_sessions": {
                    "count": len(ai_router_sessions),
                    "sessions": ai_router_sessions,
                },
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"获取统计信息失败: {str(e)}",
            "data": None,
        }
