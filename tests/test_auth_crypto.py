"""``gsuid_core.webconsole.auth_crypto`` 回归测试。

本模块为网页控制台登录/注册/改密提供应用层混合加密（X25519 ECDH + HKDF-SHA256 +
AES-256-GCM），目的是在纯 HTTP 部署下消除「密码明文上链路」的被动嗅探风险。
2026-06-15 新增，本测试覆盖其正确性与安全边界，防止后续误改回归。

覆盖矩阵：
- 正常加解密往返（前端视角加密、后端视角解密）→ 字段一致
- 加密强制开启（无开关）：明文报文 / enc!=true 一律被拒
- 篡改 ct → AES-GCM 认证失败
- 错误 key_id → 拒绝
- 缺字段 / 非 dict 载荷 → 拒绝
- ``ts`` 超出窗口 → 拒绝（防重放）
- 公钥信息稳定（同一进程多次调用一致）
- 协议常量与前端的契约锁定（PROTOCOL_ID / alg）
"""

import os
import json
import time
import base64
from typing import Any, Dict
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PublicKey,
    X25519PrivateKey,
)

from gsuid_core.webconsole.auth_crypto import (
    PROTOCOL_ID,
    TS_TOLERANCE_SECONDS,
    AuthCryptoError,
    auth_keystore,
    maybe_decrypt_auth_body,
)


@pytest.fixture(autouse=True)
def _force_zh_cn_language():
    """锁定测试语言为 zh-cn，避免运行机 LANGUAGE 配置影响错误消息断言。"""
    with patch("gsuid_core.i18n.get_lang", return_value="zh-cn"):
        yield


# ─────────────────────────────────────────────
# 前端加密辅助：用与前端一致的算法构造一个加密报文
# ─────────────────────────────────────────────


def _frontend_encrypt(
    fields: Dict[str, Any],
    *,
    ts: float | None = None,
    key_id_override: str | None = None,
    tamper_ct: bool = False,
) -> Dict[str, Any]:
    """模拟前端：取服务端公钥 → 本地 ECDH → HKDF → AES-256-GCM 加密。"""
    info = auth_keystore.public_info()
    pad = "=" * (-len(info["pubkey"]) % 4)
    server_pub = base64.urlsafe_b64decode(info["pubkey"] + pad)

    client_priv = X25519PrivateKey.generate()
    client_pub_bytes = client_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = client_priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=PROTOCOL_ID.encode("ascii"),
    ).derive(shared)

    iv = os.urandom(12)
    payload = dict(fields)
    payload["ts"] = time.time() if ts is None else ts
    ct = bytearray(AESGCM(key).encrypt(iv, json.dumps(payload).encode("utf-8"), None))

    if tamper_ct:
        # 翻转密文最后两字节，制造 AES-GCM 认证失败
        ct[-1] ^= 0xFF

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "enc": True,
        "key_id": key_id_override if key_id_override else info["key_id"],
        "client_pub": _b64(client_pub_bytes),
        "iv": _b64(iv),
        "ct": _b64(bytes(ct)),
    }


# ─────────────────────────────────────────────
# 协议常量契约：锁定前后端一致性
# ─────────────────────────────────────────────


def test_protocol_id_locked() -> None:
    # PROTOCOL_ID 同时是 HKDF info，前端必须使用完全一致的字符串
    assert PROTOCOL_ID == "gsuid-webconsole-auth/v1"


def test_ts_tolerance_is_120s() -> None:
    # 防重放窗口锁定为 120s，改动需同步前端
    assert TS_TOLERANCE_SECONDS == 120


def test_public_info_alg_is_locked() -> None:
    info = auth_keystore.public_info()
    assert info["alg"] == "x25519-aes256gcm"
    # 必备字段
    assert {"key_id", "alg", "pubkey", "fingerprint"} <= set(info.keys())


def test_public_info_is_stable_within_process() -> None:
    # 同一进程内多次取公钥应完全一致（密钥仅在启动时生成一次）
    a = auth_keystore.public_info()
    b = auth_keystore.public_info()
    assert a == b


def test_pubkey_decodes_to_32_raw_bytes() -> None:
    # X25519 原始公钥固定 32 字节
    info = auth_keystore.public_info()
    pad = "=" * (-len(info["pubkey"]) % 4)
    raw = base64.urlsafe_b64decode(info["pubkey"] + pad)
    assert len(raw) == 32


# ─────────────────────────────────────────────
# 正常路径：加密往返
# ─────────────────────────────────────────────


def test_roundtrip_login_fields() -> None:
    body = _frontend_encrypt({"email": "a@b.com", "password": "hunter2"})
    result = maybe_decrypt_auth_body(body)
    assert result["email"] == "a@b.com"
    assert result["password"] == "hunter2"
    assert "ts" in result


def test_roundtrip_preserves_unicode() -> None:
    body = _frontend_encrypt({"name": "你好世界", "password": "密码🔐"})
    result = maybe_decrypt_auth_body(body)
    assert result["name"] == "你好世界"
    assert result["password"] == "密码🔐"


def test_roundtrip_preserves_register_fields() -> None:
    body = _frontend_encrypt(
        {
            "name": "新用户",
            "email": "new@example.com",
            "password": "p@ssw0rd",
            "register_code": "ABCDEF",
        }
    )
    result = maybe_decrypt_auth_body(body)
    assert result["register_code"] == "ABCDEF"


# ─────────────────────────────────────────────
# 强制加密：明文一律被拒（无向后兼容、无开关）
# ─────────────────────────────────────────────


def test_plaintext_rejected() -> None:
    # 加密强制开启：明文报文一律被拒（不再向后兼容）
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body({"email": "x", "password": "y"})


def test_encrypted_accepted() -> None:
    # 合法加密报文正常解密
    body = _frontend_encrypt({"email": "x", "password": "y"})
    result = maybe_decrypt_auth_body(body)
    assert result["email"] == "x"


# ─────────────────────────────────────────────
# 安全边界：篡改 / 伪造 / 重放
# ─────────────────────────────────────────────


def test_tampered_ciphertext_rejected() -> None:
    body = _frontend_encrypt({"email": "x", "password": "y"}, tamper_ct=True)
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(body)


def test_unknown_key_id_rejected() -> None:
    body = _frontend_encrypt({"email": "x", "password": "y"}, key_id_override="deadbeef")
    with pytest.raises(AuthCryptoError, match="key_id"):
        maybe_decrypt_auth_body(body)


def test_stale_timestamp_rejected() -> None:
    # ts 远超容忍窗口 → 视为重放
    body = _frontend_encrypt({"email": "x", "password": "y"}, ts=time.time() - 9999)
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(body)


def test_future_timestamp_rejected() -> None:
    # ts 远在未来同样拒绝（防止把时钟拨快绕过）
    body = _frontend_encrypt({"email": "x", "password": "y"}, ts=time.time() + 9999)
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(body)


def test_timestamp_at_boundary_accepted() -> None:
    # 窗口边界（略小于容忍上限）应放行
    body = _frontend_encrypt({"email": "x", "password": "y"}, ts=time.time() - TS_TOLERANCE_SECONDS + 5)
    result = maybe_decrypt_auth_body(body)
    assert result["email"] == "x"


def test_missing_timestamp_rejected() -> None:
    # 载荷里没有 ts 字段 → 必须被拒（防重放前提）
    body = _frontend_encrypt_raw({"email": "x", "password": "y"})
    with pytest.raises(AuthCryptoError, match="ts"):
        maybe_decrypt_auth_body(body)


def test_non_dict_payload_rejected() -> None:
    # 解密后不是 JSON 对象（如纯数组）应被拒
    body = _frontend_encrypt_json_array()
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(body)


def _frontend_encrypt_raw(fields: Dict[str, Any]) -> Dict[str, Any]:
    """加密一个不含 ts 的对象载荷，用于测试「缺 ts」拒绝路径。"""
    info = auth_keystore.public_info()
    pad = "=" * (-len(info["pubkey"]) % 4)
    server_pub = base64.urlsafe_b64decode(info["pubkey"] + pad)
    client_priv = X25519PrivateKey.generate()
    client_pub_bytes = client_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = client_priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=PROTOCOL_ID.encode("ascii"),
    ).derive(shared)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(fields).encode("utf-8"), None)

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "enc": True,
        "key_id": info["key_id"],
        "client_pub": _b64(client_pub_bytes),
        "iv": _b64(iv),
        "ct": _b64(ct),
    }


def _frontend_encrypt_json_array() -> Dict[str, Any]:
    """加密一个 JSON 数组（而非对象）的载荷，用于测试非对象拒绝。"""
    info = auth_keystore.public_info()
    pad = "=" * (-len(info["pubkey"]) % 4)
    server_pub = base64.urlsafe_b64decode(info["pubkey"] + pad)
    client_priv = X25519PrivateKey.generate()
    client_pub_bytes = client_priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = client_priv.exchange(X25519PublicKey.from_public_bytes(server_pub))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=PROTOCOL_ID.encode("ascii"),
    ).derive(shared)
    iv = os.urandom(12)
    # 载荷是数组而非对象，且仍带 ts 以通过 ts 校验前的解析
    payload = json.dumps([1, 2, 3, {"ts": time.time()}]).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, payload, None)

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "enc": True,
        "key_id": info["key_id"],
        "client_pub": _b64(client_pub_bytes),
        "iv": _b64(iv),
        "ct": _b64(ct),
    }


# ─────────────────────────────────────────────
# 结构校验：缺字段 / 标志位
# ─────────────────────────────────────────────


def test_non_enc_body_rejected() -> None:
    # 没有 enc=True 标志的报文（含 enc=False）一律被拒
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body({"email": "x", "password": "y", "enc": False})


def test_encrypted_body_missing_fields_rejected() -> None:
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body({"enc": True, "key_id": "x"})


def test_malformed_client_pubkey_rejected() -> None:
    body = _frontend_encrypt({"email": "x", "password": "y"})
    body["client_pub"] = "not-valid-base64-!!!"
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(body)
