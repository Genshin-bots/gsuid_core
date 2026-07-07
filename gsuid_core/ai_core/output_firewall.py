"""出戏防火墙：AI 输出侧的强制后处理闸门（§D）。

见 ``docs/SESSION_LOG_SECURITY_FINDINGS_20260707.md`` §D.4。把 system prompt 里的
"出戏防火墙"从"建议"变成代码强制点——AI 回复下发前过一遍分类词库，命中即建议重说。

两条输出路径共用本模块：
- ``send_message_by_ai``（工具，有 return 通道）：命中 → return 警告让模型重发；
- ``send_chat_result``（主输出路径，无 return）：命中 → 不发该段 + 注入重说反馈。

**设计核心**：因为是"命中即重说"而非"永久封禁"，词库可激进高召回、宁可偶尔错杀——
错杀只多生成一次（用户无感），漏杀才是事故。故不追求正则完备。
"""

import re
from typing import Any, Dict, List, Tuple, Optional
from dataclasses import dataclass

from gsuid_core.logger import logger
from gsuid_core.ai_core.content_guard import normalize_for_match

# ── 分类词库 ────────────────────────────────────────────────────────
# 规范化后匹配（吃掉"M i M o"式规避）。部署者可经 ai_config.output_firewall_extra_terms 补充。

# 模型 / 厂商名（最高危：公开群聊暴露即事故）
_MODEL_TERMS: Tuple[str, ...] = (
    "mimo",
    "minimax",
    "gpt",
    "claude",
    "gemini",
    "通义",
    "千问",
    "qwen",
    "文心",
    "豆包",
    "星火",
    "kimi",
    "deepseek",
    "深度求索",
    "小爱",
    "siri",
    "openai",
    "anthropic",
    "小米大模型",
    "chatgpt",
    "llama",
)

# 系统 / 技术术语（出戏痕迹）
_SYSTEM_TERMS: Tuple[str, ...] = (
    "systemprompt",
    "系统提示词",
    "上下文窗口",
    "traceback",
    "api密钥",
    "apikey",
    "数据库表",
    "供应商",
)

# 独立正则（原文匹配，保留边界 / 结构语义）
_MODEL_ATTRIB_RE = re.compile(r"由.{0,10}(开发|训练|研发|提供|打造)")
_AI_SELFREF_RE = re.compile(
    r"我(是|叫|本质上是|其实是)(一个|个)?.{0,6}"
    r"(ai|人工智能|语言模型|大模型|聊天机器人|机器人|程序|模型)",
    re.IGNORECASE,
)
_SK_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{8,}")
_ERR_CODE_RE = re.compile(r"(错误码|报错码|error\s*code)[\s:：]*\d+", re.IGNORECASE)


@dataclass
class FirewallHit:
    """出戏命中：类别 + 命中片段（供警告文案与日志）。"""

    category: str  # "model_identity" | "system_term" | "ai_selfref"
    matched: List[str]


def _extra_terms() -> Tuple[str, ...]:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    data = ai_config.get_config("output_firewall_extra_terms").data
    if isinstance(data, list):
        return tuple(str(x) for x in data if str(x).strip())
    return ()


def check_ooc(text: str, tier: str = "roleplay") -> Optional[FirewallHit]:
    """检测 AI 输出是否命中出戏红线。``tier="plain"`` 直接放行（那类节点允许暴露系统信息）。

    命中返回 ``FirewallHit``，否则 None。规范化匹配词库 + 独立正则。
    """
    # 注意：tier="plain" 目前生产无调用方，为将来非角色扮演出口预留（尚未接线）
    if not text or tier == "plain":
        return None

    norm = normalize_for_match(text)
    model_hits = [
        w for w in (*_MODEL_TERMS, *_extra_terms()) if normalize_for_match(w) and normalize_for_match(w) in norm
    ]
    if _AI_SELFREF_RE.search(text):
        return FirewallHit(category="ai_selfref", matched=["AI自指"])
    if model_hits or _MODEL_ATTRIB_RE.search(text):
        matched = model_hits or ["由…开发"]
        return FirewallHit(category="model_identity", matched=matched)
    system_hits = [w for w in _SYSTEM_TERMS if normalize_for_match(w) in norm]
    if _SK_KEY_RE.search(text):
        system_hits.append("密钥")
    if _ERR_CODE_RE.search(text):
        system_hits.append("错误码")
    if system_hits:
        return FirewallHit(category="system_term", matched=system_hits)
    return None


def is_enabled() -> bool:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return bool(ai_config.get_config("output_firewall_enable").data)


def build_rewrite_warning(hit: FirewallHit) -> str:
    """给模型的重说警告（工具 return / 反馈注入共用）。"""
    return (
        f"⛔ 你要发送的内容命中出戏红线【类别：{hit.category}，命中：{'、'.join(hit.matched[:4])}】，"
        "会破坏角色扮演。请用【纯角色口吻】重写这条消息，去掉任何模型名 / AI 身份 / 系统术语 / "
        "报错信息后再发送——绝不透露你的模型、供应商、版本或任何系统实现细节。"
    )


# 连续重说仍命中时的角色化兜底（避免死循环）——调用方在第 N 次命中后改用它替换。
PERSONA_FALLBACK_TEXT = "唔…这个不太想说呢…"


def gate_warn_once(extra: Dict[str, Any], text: str) -> Optional[str]:
    """ "提醒一次→重说→放行"闸门（§D.4）：供有 return 通道的工具路径复用。

    同轮首次命中返回重写警告（模型据此重写重发）；同轮再命中返回 None 放行——
    防"警告↔重试"死循环，误杀只值一次重写。``extra`` 是 ``ToolContext.extra``
    （含 gs_agent 每轮写入的 turn_id）；无 turn_id 的后台链路每次都警告。
    """
    hit = check_ooc(text)
    if hit is None:
        return None
    turn_id = str(extra.get("turn_id", ""))
    warn_key = f"ooc_warned:{turn_id}"
    if turn_id and extra.get(warn_key):
        logger.warning(f"[OutputFirewall] 重写后仍命中 {hit.category}: {hit.matched}，本轮放行")
        return None
    if turn_id:
        extra[warn_key] = True
    logger.warning(f"[OutputFirewall] 命中出戏红线 {hit.category}: {hit.matched}，要求重写")
    return build_rewrite_warning(hit)


def scrub_or_fallback(text: str, tier: str = "roleplay") -> Tuple[str, bool]:
    """无反馈通道路径的末端兜底：命中则整体替换为角色化兜底文本。

    返回 ``(输出文本, 是否被拦截替换)``。用于重说闭环兜底或不便重说的场景。
    """
    if check_ooc(text, tier) is None:
        return text, False
    return PERSONA_FALLBACK_TEXT, True
