"""落盘前脱敏：结构形态（密钥/令牌/Cookie），无业务域词。"""

from __future__ import annotations

import re

# 形态信号：sk- / Bearer / JWT / 带标签的 hex 密钥 / cookie；不裸杀 64-hex 摘要
_SECRET_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    re.compile(r"(?i)\b(cookie|set-cookie)\s*[:=]\s*[^\n]{12,}"),
    # 仅带密钥/哈希标签的长 hex，避免误伤工具 checksum
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\s*[:=]\s*['\"]?[A-Fa-f0-9]{32,}\b"),
    re.compile(r"(?i)\b(sha256|checksum|digest)\s*[:=]\s*['\"]?[A-Fa-f0-9]{64}\b"),
    re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/]{200,}={0,2}"),  # 巨型 data-uri
)

_REDACT = "[REDACTED]"


def sanitize_for_persist(text: str) -> tuple[str, int]:
    """返回 (脱敏后文本, 替换次数)。"""
    if not text:
        return text, 0
    out = text
    n = 0
    for pat in _SECRET_RES:
        out, c = pat.subn(_REDACT, out)
        n += c
    return out, n
