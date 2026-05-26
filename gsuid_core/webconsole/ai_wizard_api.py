"""
AI Wizard APIs

提供 AI 配置向导功能的 RESTful APIs
用于前端配置向导，显示 AI 功能状态和配置缺失项
"""

from typing import Any, Dict, List

from fastapi import Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.configs.models import (
    parse_provider_config_name,
)
from gsuid_core.ai_core.configs.ai_config import (
    ai_config,
    exa_config,
    memory_config,
    tavily_config,
)


def _check_model_vision_support(provider: str, config_name: str) -> Dict[str, Any]:
    """
    检查模型是否支持视觉/图片理解

    Args:
        provider: 提供商类型 (openai/anthropic)
        config_name: 配置名称

    Returns:
        包含视觉支持信息的字典
    """
    if not provider or not config_name:
        return {
            "supported": False,
            "model_name": "未配置",
            "note": "模型未配置",
        }

    try:
        if provider == "openai":
            from gsuid_core.ai_core.configs.openai_config import get_openai_config

            config = get_openai_config(config_name)
            model_name = config.get_config("model_name").data if config else ""
        elif provider == "anthropic":
            from gsuid_core.ai_core.configs.anthropic_config import get_anthropic_config

            config = get_anthropic_config(config_name)
            model_name = config.get_config("model_name").data if config else ""
        else:
            return {
                "supported": False,
                "model_name": "未知",
                "note": f"不支持的 provider: {provider}",
            }

        # 常见支持视觉的模型关键词
        vision_keywords = [
            "vision",
            "vl",
            "image",
            "gpt-4o",
            "gpt-4-turbo",
            "claude-3-opus",
            "claude-3-sonnet",
            "claude-3-5",
            "claude-3-haiku",
            "gemini",
            "qwen-vl",
            "qwen2-vl",
        ]

        has_vision = any(kw in model_name.lower() for kw in vision_keywords)

        return {
            "supported": has_vision,
            "model_name": model_name,
            "note": "支持图片理解" if has_vision else "不支持图片理解",
        }
    except Exception:
        return {
            "supported": False,
            "model_name": "未知",
            "note": "无法获取模型信息",
        }


def _check_vlm_fallback_config() -> Dict[str, Any]:
    """
    检查图片转述 VLM 模型的备用配置

    Returns:
        包含 VLM 备用配置信息的字典
    """
    provider = ai_config.get_config("image_understand_provider").data

    # 如果是 MCP，检查是否有可用的 MCP 图片理解工具
    if provider == "MCP":
        try:
            from gsuid_core.ai_core.mcp.config_manager import mcp_config_manager

            # 获取所有 MCP 配置
            all_configs = mcp_config_manager.list_configs()
            image_tools = []

            for config in all_configs:
                if config.get("enabled", False):
                    tools = config.get("tools", [])
                    for tool in tools:
                        tool_name = tool.get("name", "")
                        # 常见的图片理解 MCP 工具关键词
                        if any(kw in tool_name.lower() for kw in ["image", "vision", "ocr", "图片", "视觉"]):
                            image_tools.append(
                                {
                                    "config_id": config.get("config_id", ""),
                                    "tool_name": tool_name,
                                }
                            )

            return {
                "configured": len(image_tools) > 0,
                "provider": provider,
                "tools": image_tools,
                "note": f"已配置 {len(image_tools)} 个图片理解 MCP 工具" if image_tools else "未配置图片理解 MCP 工具",
            }
        except Exception:
            return {
                "configured": False,
                "provider": provider,
                "tools": [],
                "note": "无法获取 MCP 配置",
            }

    return {
        "configured": provider != "MCP",
        "provider": provider,
        "tools": [],
        "note": f"使用 {provider} 作为图片理解服务",
    }


def _check_websearch_config() -> Dict[str, Any]:
    """
    检查 Web Search 配置状态

    Returns:
        包含 web search 配置信息的字典
    """
    provider = ai_config.get_config("websearch_provider").data

    result: Dict[str, Any] = {
        "provider": provider,
        "configured": False,
        "issues": [],
    }

    if provider == "Tavily":
        api_keys = tavily_config.get_config("api_key").data
        if not api_keys or (isinstance(api_keys, list) and len(api_keys) == 0):
            result["issues"].append("Tavily API Key 未配置")
        elif isinstance(api_keys, list) and all(not k for k in api_keys):
            result["issues"].append("Tavily API Key 为空")
        else:
            result["configured"] = True
            result["note"] = f"已配置 {len([k for k in api_keys if k])} 个 Tavily API Key"
    elif provider == "Exa":
        api_keys = exa_config.get_config("api_key").data
        if not api_keys or (isinstance(api_keys, list) and len(api_keys) == 0):
            result["issues"].append("Exa API Key 未配置")
        elif isinstance(api_keys, list) and all(not k for k in api_keys):
            result["issues"].append("Exa API Key 为空")
        else:
            result["configured"] = True
            result["note"] = f"已配置 {len([k for k in api_keys if k])} 个 Exa API Key"
    elif provider == "MCP":
        # MCP 作为 web search 提供方，检查是否有相关工具
        try:
            from gsuid_core.ai_core.mcp.config_manager import mcp_config_manager

            all_configs = mcp_config_manager.list_configs()
            search_tools: List[Dict[str, str]] = []

            for config in all_configs:
                if config.get("enabled", False):
                    tools = config.get("tools", [])
                    for tool in tools:
                        tool_name = tool.get("name", "")
                        if any(kw in tool_name.lower() for kw in ["search", "web", "google", "bing", "搜索", "网页"]):
                            search_tools.append(
                                {
                                    "config_id": config.get("config_id", ""),
                                    "tool_name": tool_name,
                                }
                            )

            if search_tools:
                result["configured"] = True
                result["tools"] = search_tools
                result["note"] = f"已配置 {len(search_tools)} 个搜索 MCP 工具"
            else:
                result["issues"].append("未配置搜索 MCP 工具")
        except Exception as e:
            result["issues"].append(f"无法获取 MCP 配置: {str(e)}")
    else:
        result["issues"].append(f"未知的搜索提供方: {provider}")

    if not result.get("note"):
        result["note"] = result["issues"][0] if result["issues"] else "未配置"

    return result


def _check_embedding_config() -> Dict[str, Any]:
    """
    检查 Embedding 模型配置状态

    Returns:
        包含 embedding 配置信息的字典
    """
    from gsuid_core.ai_core.configs.ai_config import local_embedding_config, openai_embedding_config

    provider = ai_config.get_config("embedding_provider").data

    result: Dict[str, Any] = {
        "provider": provider,
        "configured": False,
        "issues": [],
        "model_name": "",
    }

    if provider == "local":
        try:
            model_name = local_embedding_config.get_config("embedding_model_name").data
            result["model_name"] = model_name
            result["configured"] = True
            result["note"] = f"使用本地嵌入模型: {model_name}"
        except Exception as e:
            result["issues"].append(f"无法获取本地嵌入配置: {str(e)}")
    elif provider == "openai":
        try:
            base_url = openai_embedding_config.get_config("base_url").data
            api_keys = openai_embedding_config.get_config("api_key").data
            model_name = openai_embedding_config.get_config("embedding_model").data

            result["model_name"] = model_name

            if not base_url:
                result["issues"].append("OpenAI 嵌入 API URL 未配置")
            if not api_keys or (isinstance(api_keys, list) and len(api_keys) == 0):
                result["issues"].append("OpenAI API Key 未配置")
            elif isinstance(api_keys, list) and all(not k for k in api_keys):
                result["issues"].append("OpenAI API Key 为空")
            else:
                result["configured"] = True
                result["note"] = f"使用 OpenAI 嵌入模型: {model_name}"

            if not result.get("note"):
                result["note"] = result["issues"][0]
        except Exception as e:
            result["issues"].append(f"无法获取 OpenAI 嵌入配置: {str(e)}")
    else:
        result["issues"].append(f"未知的嵌入提供方: {provider}")

    if not result.get("note"):
        result["note"] = result["issues"][0] if result["issues"] else "未配置"

    return result


def _check_persona_and_enable_range() -> Dict[str, Any]:
    """
    检查人格配置和AI启用范围（合并版）

    Returns:
        包含人格配置和AI启用范围信息的字典
        - persona: 人格列表及每个人格的详细启用范围
        - user_ai_range: 全局用户级别的AI启用范围（白名单/黑名单）
    """
    try:
        from gsuid_core.ai_core.persona.config import persona_config_manager

        all_personas = persona_config_manager.get_all_configs()

        persona_list: List[Dict[str, Any]] = []
        inspect_enabled_count = 0
        enabled_count = 0

        for persona_name, config in all_personas.items():
            try:
                ai_mode = config.get_config("ai_mode").data
                inspect_interval = config.get_config("inspect_interval").data
                scope = config.get_config("scope").data
                target_groups = config.get_config("target_groups").data

                has_inspect = "定时巡检" in ai_mode if ai_mode else False
                if has_inspect:
                    inspect_enabled_count += 1

                # 判断该人格是否启用
                is_enabled = scope != "disabled"
                if is_enabled:
                    enabled_count += 1

                # 生成范围描述
                if scope == "disabled":
                    scope_desc = "已禁用"
                elif scope == "global":
                    scope_desc = "全部群聊"
                else:  # specific
                    if target_groups and len(target_groups) > 0:
                        scope_desc = f"限定 {len(target_groups)} 个群聊"
                    else:
                        scope_desc = "限定群聊（未配置）"
                    scope_desc += f" ({(scope)})"

                persona_list.append(
                    {
                        "name": persona_name,
                        "ai_mode": ai_mode if ai_mode else [],
                        "inspect_interval": inspect_interval if has_inspect else None,
                        "has_inspect": has_inspect,
                        "scope": scope,
                        "target_groups": target_groups if target_groups else [],
                        "is_enabled": is_enabled,
                        "scope_desc": scope_desc,
                    }
                )
            except Exception as e:
                persona_list.append(
                    {
                        "name": persona_name,
                        "ai_mode": [],
                        "inspect_interval": None,
                        "has_inspect": False,
                        "scope": "unknown",
                        "target_groups": [],
                        "is_enabled": False,
                        "scope_desc": f"获取配置失败: {str(e)}",
                    }
                )

        # 获取全局用户级别的AI启用范围
        white_list = ai_config.get_config("white_list").data
        black_list = ai_config.get_config("black_list").data

        if white_list and len(white_list) > 0:
            user_range_mode = "white_list"
            user_range_desc = f"白名单模式 ({len(white_list)} 个用户)"
        elif black_list and len(black_list) > 0:
            user_range_mode = "black_list"
            user_range_desc = f"黑名单模式 ({len(black_list)} 个用户)"
        else:
            user_range_mode = "all"
            user_range_desc = "全部用户可用"

        return {
            "persona": {
                "persona_count": len(persona_list),
                "enabled_count": enabled_count,
                "inspect_enabled_count": inspect_enabled_count,
                "personas": persona_list,
                "configured": len(persona_list) > 0,
                "note": f"共 {len(persona_list)} 个人格，{enabled_count} 个已启用，"
                f"{inspect_enabled_count} 个启用了定时巡检",
            },
            "user_ai_range": {
                "mode": user_range_mode,
                "mode_desc": user_range_desc,
                "white_list": white_list if white_list else [],
                "black_list": black_list if black_list else [],
                "note": user_range_desc,
            },
        }
    except Exception as e:
        return {
            "persona": {
                "persona_count": 0,
                "enabled_count": 0,
                "inspect_enabled_count": 0,
                "personas": [],
                "configured": False,
                "issues": [f"无法获取人格配置: {str(e)}"],
                "note": f"获取人格配置失败: {str(e)}",
            },
            "user_ai_range": {
                "mode": "unknown",
                "mode_desc": f"获取配置失败: {str(e)}",
                "white_list": [],
                "black_list": [],
                "note": f"获取配置失败: {str(e)}",
            },
        }


def _analyze_missing_configs(wizard_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    分析缺失的配置项，确定哪些导致 AI 不可用

    Args:
        wizard_data: 向导完整数据

    Returns:
        缺失配置项列表
    """
    issues: List[Dict[str, Any]] = []

    # 检查 AI 总开关
    if not wizard_data.get("ai_enabled", False):
        issues.append(
            {
                "category": "ai_enable",
                "item": "AI 总开关",
                "severity": "critical",
                "message": "AI 服务未启用",
                "recommendation": "在 AI 配置中启用 AI 服务",
            }
        )

    # 检查高级模型配置
    high_level = wizard_data.get("high_level_model", {})
    if not high_level.get("configured", False):
        issues.append(
            {
                "category": "model",
                "item": "高级任务模型",
                "severity": "critical",
                "message": "高级任务模型未配置",
                "recommendation": "在 AI 模型配置中添加高级任务模型配置",
            }
        )

    # 检查低级模型配置
    low_level = wizard_data.get("low_level_model", {})
    if not low_level.get("configured", False):
        issues.append(
            {
                "category": "model",
                "item": "低级任务模型",
                "severity": "critical",
                "message": "低级任务模型未配置",
                "recommendation": "在 AI 模型配置中添加低级任务模型配置",
            }
        )

    # 检查图片理解
    if not wizard_data.get("vision_support", {}).get("available", False):
        issues.append(
            {
                "category": "vision",
                "item": "图片理解能力",
                "severity": "warning",
                "message": "当前模型不支持图片理解，且未配置 VLM 备用方案",
                "recommendation": "配置支持视觉的模型或添加 MCP 图片理解工具",
            }
        )

    # 检查人格配置
    persona = wizard_data.get("persona", {})
    if not persona.get("configured", False):
        issues.append(
            {
                "category": "persona",
                "item": "人格配置",
                "severity": "warning",
                "message": "未配置任何人格",
                "recommendation": "在人格管理中添加人格配置",
            }
        )

    # 检查 Web Search
    websearch = wizard_data.get("web_search", {})
    if not websearch.get("configured", False):
        issues.append(
            {
                "category": "websearch",
                "item": "网络搜索",
                "severity": "warning",
                "message": f"网络搜索未配置: {websearch.get('note', '未知错误')}",
                "recommendation": "配置 Tavily/Exa API Key 或配置 MCP 搜索工具",
            }
        )

    # 检查 Embedding
    embedding = wizard_data.get("embedding", {})
    if not embedding.get("configured", False):
        issues.append(
            {
                "category": "embedding",
                "item": "嵌入模型",
                "severity": "warning",
                "message": f"嵌入模型未配置: {embedding.get('note', '未知错误')}",
                "recommendation": "配置本地嵌入模型或 OpenAI 嵌入模型",
            }
        )

    return issues


# ==================== AI 配置向导 API ====================


@app.get("/api/ai/wizard/status")
async def get_ai_wizard_status(_: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取 AI 配置状态向导数据

    该 API 整合了 AI 配置的各个方面信息，帮助前端展示配置向导界面。
    返回数据中会标记缺失的配置项，前端可根据 severity 字段进行标红处理。

    Returns:
        status: 0 成功
        data: {
            # AI 基础状态
            "ai_enabled": bool,              # AI 总开关是否启用
            "ai_enable_range": {...},         # AI 启用范围配置

            # 模型配置
            "high_level_model": {
                "configured": bool,            # 是否已配置
                "provider": str,               # 提供商
                "config_name": str,            # 配置名称
                "model_name": str,            # 模型名称
                "full_name": str,              # provider++config_name 格式
            },
            "low_level_model": {
                "configured": bool,
                "provider": str,
                "config_name": str,
                "model_name": str,
                "full_name": str,
            },

            # 图片理解
            "vision_support": {
                "available": bool,             # 是否有图片理解能力
                "high_level_vision": {...},    # 高级模型视觉支持情况
                "low_level_vision": {...},     # 低级模型视觉支持情况
                "vlm_fallback": {...},         # VLM 备用方案
            },

            # 人格配置
            "persona": {
                "configured": bool,
                "persona_count": int,
                "personas": [...],
                "inspect_enabled_count": int,
            },

            # 记忆配置
            "memory": {
                "enabled": bool,              # 记忆功能是否启用
                "memory_mode": [...],         # 记忆路径列表
                "memory_session": str,        # 被动感知范围
            },

            # 嵌入模型
            "embedding": {...},

            # Web Search
            "web_search": {...},

            # 缺失配置分析
            "missing_configs": [             # 未配置或配置有问题的项
                {
                    "category": str,          # 问题分类
                    "item": str,              # 问题项
                    "severity": str,          # critical/warning/info
                    "message": str,           # 问题描述
                    "recommendation": str,    # 修复建议
                }
            ],
        }
    """
    # 获取 AI 总开关
    ai_enabled = ai_config.get_config("enable").data

    # 获取高级/低级模型配置
    high_level_full_name = ai_config.get_config("high_level_provider_config_name").data
    low_level_full_name = ai_config.get_config("low_level_provider_config_name").data

    high_level_model: Dict[str, Any] = {"configured": False}
    low_level_model: Dict[str, Any] = {"configured": False}

    # 解析高级模型配置
    if high_level_full_name and "++" in high_level_full_name:
        provider, config_name = parse_provider_config_name(high_level_full_name)
        high_level_model = {
            "configured": True,
            "provider": provider,
            "config_name": config_name,
            "model_name": "",
            "full_name": high_level_full_name,
        }
        try:
            if provider == "openai":
                from gsuid_core.ai_core.configs.openai_config import get_openai_config

                config = get_openai_config(config_name)
                if config:
                    high_level_model["model_name"] = config.get_config("model_name").data
            elif provider == "anthropic":
                from gsuid_core.ai_core.configs.anthropic_config import get_anthropic_config

                config = get_anthropic_config(config_name)
                if config:
                    high_level_model["model_name"] = config.get_config("model_name").data
        except Exception:
            pass

    # 解析低级模型配置
    if low_level_full_name and "++" in low_level_full_name:
        provider, config_name = parse_provider_config_name(low_level_full_name)
        low_level_model = {
            "configured": True,
            "provider": provider,
            "config_name": config_name,
            "model_name": "",
            "full_name": low_level_full_name,
        }
        try:
            if provider == "openai":
                from gsuid_core.ai_core.configs.openai_config import get_openai_config

                config = get_openai_config(config_name)
                if config:
                    low_level_model["model_name"] = config.get_config("model_name").data
            elif provider == "anthropic":
                from gsuid_core.ai_core.configs.anthropic_config import get_anthropic_config

                config = get_anthropic_config(config_name)
                if config:
                    low_level_model["model_name"] = config.get_config("model_name").data
        except Exception:
            pass

    # 检查视觉支持
    high_level_vision = _check_model_vision_support(
        str(high_level_model.get("provider", "")),
        str(high_level_model.get("config_name", "")),
    )
    low_level_vision = _check_model_vision_support(
        str(low_level_model.get("provider", "")),
        str(low_level_model.get("config_name", "")),
    )

    vision_available = high_level_vision.get("supported", False) or low_level_vision.get("supported", False)

    # VLM 备用方案
    vlm_fallback = _check_vlm_fallback_config()
    if not vision_available and vlm_fallback.get("configured", False):
        vision_available = True

    # 获取人格配置和AI启用范围（合并版）
    persona_and_range = _check_persona_and_enable_range()

    # 获取记忆配置
    memory_enabled = ai_config.get_config("enable_memory").data
    memory_mode = memory_config.get_config("memory_mode").data
    memory_session = memory_config.get_config("memory_session").data

    # 获取嵌入模型配置
    embedding_data = _check_embedding_config()

    # 获取 Web Search 配置
    websearch_data = _check_websearch_config()

    # 构建完整数据
    wizard_data: Dict[str, Any] = {
        "ai_enabled": ai_enabled,
        "ai_enable_range": persona_and_range.get("user_ai_range", {}),
        "high_level_model": high_level_model,
        "low_level_model": low_level_model,
        "vision_support": {
            "available": vision_available,
            "high_level_vision": high_level_vision,
            "low_level_vision": low_level_vision,
            "vlm_fallback": vlm_fallback,
        },
        "persona": persona_and_range.get("persona", {}),
        "memory": {
            "enabled": memory_enabled,
            "memory_mode": memory_mode if memory_mode else [],
            "memory_session": memory_session,
        },
        "embedding": embedding_data,
        "web_search": websearch_data,
    }

    # 分析缺失配置
    missing_configs = _analyze_missing_configs(wizard_data)

    # 添加严重程度标记
    has_critical = any(c.get("severity") == "critical" for c in missing_configs)
    has_warning = any(c.get("severity") == "warning" for c in missing_configs)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            **wizard_data,
            "missing_configs": missing_configs,
            "summary": {
                "total_issues": len(missing_configs),
                "critical_count": sum(1 for c in missing_configs if c.get("severity") == "critical"),
                "warning_count": sum(1 for c in missing_configs if c.get("severity") == "warning"),
                "info_count": sum(1 for c in missing_configs if c.get("severity") == "info"),
                "ai_usable": ai_enabled
                and high_level_model.get("configured", False)
                and low_level_model.get("configured", False)
                and not has_critical,
                "note": "AI 不可用，请修复所有 critical 问题"
                if has_critical
                else "AI 可用，但存在警告问题"
                if has_warning
                else "AI 配置完整",
            },
        },
    }


@app.get("/api/ai/wizard/checklist")
async def get_ai_wizard_checklist(_: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取 AI 配置检查清单（简化版）

    该 API 返回一个扁平化的检查清单，用于快速展示配置状态。

    Returns:
        status: 0 成功
        data: {
            "items": [
                {
                    "id": str,                # 检查项 ID
                    "category": str,         # 分类
                    "name": str,              # 检查项名称
                    "status": str,            # ok/warning/error
                    "value": Any,             # 当前值
                    "message": str,           # 状态消息
                }
            ],
            "overall_status": str,            # overall_ok/overall_warning/overall_error
            "usable": bool,                   # AI 是否可用
        }
    """
    items: List[Dict[str, Any]] = []

    # AI 总开关
    ai_enabled = ai_config.get_config("enable").data
    items.append(
        {
            "id": "ai_enable",
            "category": "基础",
            "name": "AI 服务",
            "status": "ok" if ai_enabled else "error",
            "value": ai_enabled,
            "message": "已启用" if ai_enabled else "未启用",
        }
    )

    # 高级模型
    high_level_full_name = ai_config.get_config("high_level_provider_config_name").data
    items.append(
        {
            "id": "high_level_model",
            "category": "模型",
            "name": "高级任务模型",
            "status": "ok" if high_level_full_name else "error",
            "value": high_level_full_name,
            "message": high_level_full_name if high_level_full_name else "未配置",
        }
    )

    # 低级模型
    low_level_full_name = ai_config.get_config("low_level_provider_config_name").data
    items.append(
        {
            "id": "low_level_model",
            "category": "模型",
            "name": "低级任务模型",
            "status": "ok" if low_level_full_name else "error",
            "value": low_level_full_name,
            "message": low_level_full_name if low_level_full_name else "未配置",
        }
    )

    # 图片理解
    high_level_provider = ""
    high_level_config_name = ""
    low_level_provider = ""
    low_level_config_name = ""

    if high_level_full_name and "++" in high_level_full_name:
        high_level_provider, high_level_config_name = parse_provider_config_name(high_level_full_name)
    if low_level_full_name and "++" in low_level_full_name:
        low_level_provider, low_level_config_name = parse_provider_config_name(low_level_full_name)

    high_level_vision = _check_model_vision_support(high_level_provider, high_level_config_name)
    low_level_vision = _check_model_vision_support(low_level_provider, low_level_config_name)
    vlm_fallback = _check_vlm_fallback_config()

    has_vision = (
        high_level_vision.get("supported") or low_level_vision.get("supported") or vlm_fallback.get("configured")
    )

    items.append(
        {
            "id": "vision",
            "category": "功能",
            "name": "图片理解",
            "status": "ok" if has_vision else "warning",
            "value": has_vision,
            "message": "可用" if has_vision else "不支持",
        }
    )

    # 人格和AI启用范围（合并版）
    persona_and_range = _check_persona_and_enable_range()
    persona_info = persona_and_range.get("persona", {})
    user_range_info = persona_and_range.get("user_ai_range", {})

    # 人格配置
    items.append(
        {
            "id": "persona",
            "category": "人格",
            "name": "人格配置",
            "status": "ok" if persona_info.get("configured") else "warning",
            "value": persona_info.get("persona_count", 0),
            "message": persona_info.get("note", "未配置"),
        }
    )

    # 记忆
    memory_enabled = ai_config.get_config("enable_memory").data
    memory_mode = memory_config.get_config("memory_mode").data
    memory_session = memory_config.get_config("memory_session").data

    # 构建记忆模式描述
    memory_mode_desc = ""
    if memory_mode:
        mode_names = {
            "被动感知": "被动记忆",
            "主动会话": "主动记忆",
        }
        mode_list = [str(mode_names.get(m, m)) for m in memory_mode if m in mode_names]
        memory_mode_desc = " + ".join(mode_list) if mode_list else "无"
    else:
        memory_mode_desc = "无"

    # 构建记忆范围描述
    is_all_groups = memory_session != "按人格配置"
    memory_session_desc = "按人格配置" if not is_all_groups else "全部群聊"

    # 如果是全部群聊，添加警告
    memory_note = f"{memory_mode_desc} | {memory_session_desc}" if memory_enabled else "未启用"
    if memory_enabled and is_all_groups:
        memory_note += " ⚠️"

    items.append(
        {
            "id": "memory",
            "category": "记忆",
            "name": "记忆功能",
            "status": "warning" if (memory_enabled and is_all_groups) else ("ok" if memory_enabled else "warning"),
            "value": {
                "enabled": memory_enabled,
                "memory_mode": memory_mode if memory_mode else [],
                "memory_session": memory_session,
                "is_all_groups_warning": is_all_groups,
            },
            "message": memory_note,
        }
    )

    # 嵌入模型
    embedding_data = _check_embedding_config()
    items.append(
        {
            "id": "embedding",
            "category": "RAG",
            "name": "嵌入模型",
            "status": "ok" if embedding_data.get("configured") else "error",
            "value": embedding_data.get("model_name", ""),
            "message": embedding_data.get("note", "未配置"),
        }
    )

    # Web Search
    websearch_data = _check_websearch_config()
    items.append(
        {
            "id": "websearch",
            "category": "工具",
            "name": "网络搜索",
            "status": "ok" if websearch_data.get("configured") else "warning",
            "value": websearch_data.get("provider", ""),
            "message": websearch_data.get("note", "未配置"),
        }
    )

    # AI 用户级启用范围
    items.append(
        {
            "id": "ai_range",
            "category": "基础",
            "name": "AI 用户范围",
            "status": "ok",
            "value": user_range_info.get("mode", ""),
            "message": user_range_info.get("mode_desc", ""),
        }
    )

    # 计算整体状态
    error_count = sum(1 for i in items if i["status"] == "error")
    warning_count = sum(1 for i in items if i["status"] == "warning")

    if error_count > 0:
        overall_status = "overall_error"
    elif warning_count > 0:
        overall_status = "overall_warning"
    else:
        overall_status = "overall_ok"

    # 判断 AI 是否可用
    usable = (
        ai_enabled
        and high_level_full_name
        and low_level_full_name
        and embedding_data.get("configured")
        and error_count == 0
    )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "items": items,
            "overall_status": overall_status,
            "usable": usable,
            "summary": {
                "total": len(items),
                "ok": sum(1 for i in items if i["status"] == "ok"),
                "warning": warning_count,
                "error": error_count,
            },
        },
    }
