"""确认码：生成、哈希、形状判断。明文只活在私聊发送的那一次。"""

import hmac
import hashlib
import secrets

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 6


def looks_like_confirm_code(text: str) -> bool:
    """整句去掉空白后是否像一枚确认码。多一个字、包在句子里，都不算。"""
    body = text.strip()
    if len(body) != _CODE_LEN:
        return False
    upper = body.upper()
    if any(ch not in _ALPHABET for ch in upper):
        return False
    return any(ch.isalpha() for ch in upper) and any(ch.isdigit() for ch in upper)


def draw_confirm_code() -> str:
    """抽一枚至少含 1 个字母和 1 个数字的确认码。"""
    while True:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
        if any(ch.isalpha() for ch in code) and any(ch.isdigit() for ch in code):
            return code


def hash_confirm_code(code: str) -> str:
    """先转大写再 SHA-256。比对只拿这个十六进制串。"""
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def hashes_equal(left: str, right: str) -> bool:
    if len(left) != len(right) or not left:
        return False
    return hmac.compare_digest(left, right)
