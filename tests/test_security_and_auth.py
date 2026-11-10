"""WebConsole 安全路径校验、身份鉴权与加密通信测试。"""

from __future__ import annotations

import os
import json
import time
import base64
from typing import Any, Dict
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey, X25519PrivateKey

from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    confine_to_root,
    is_safe_relpath,
    is_safe_filename,
)
from gsuid_core.webconsole.auth_crypto import (
    PROTOCOL_ID,
    AuthCryptoError,
    auth_keystore,
    maybe_decrypt_auth_body,
)


@pytest.fixture(autouse=True)
def _force_zh_cn_language():
    with patch("gsuid_core.i18n.get_lang", return_value="zh-cn"):
        yield


def _frontend_encrypt(
    fields: Dict[str, Any],
    *,
    ts: float | None = None,
    tamper_ct: bool = False,
) -> Dict[str, Any]:
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
        ct[-1] ^= 0xFF

    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {
        "enc": True,
        "key_id": info["key_id"],
        "client_pub": _b64(client_pub_bytes),
        "iv": _b64(iv),
        "ct": _b64(bytes(ct)),
    }


def test_path_safety_confinement_and_filename_guards(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    nested = root / "assets" / "js"
    nested.mkdir(parents=True)
    target = nested / "index.js"
    target.write_text("1", encoding="utf-8")
    cn = root / "下载.jpg"
    cn.write_bytes(b"x")

    for bad in ("../../../config.json", "C:/Windows/win.ini", "/etc/passwd"):
        with pytest.raises(PathEscapeError):
            safe_join(root, bad)
    with pytest.raises(PathEscapeError):
        safe_join(root, "..", "..", "config.json")

    assert safe_join(root, "assets/js/index.js") == target.resolve()
    assert safe_join(root, "下载.jpg") == cn.resolve()
    assert confine_to_root(str(target), root) == target.resolve()
    with pytest.raises(PathEscapeError):
        confine_to_root(str(tmp_path / "outside.txt"), root)

    assert is_safe_filename("avatar.png")
    assert not is_safe_filename("../config.json") and not is_safe_filename("a/b.png")
    assert not is_safe_relpath("assets/../../../data/config.json")


def test_plugin_store_seeded_order_is_stable_across_pages() -> None:
    from gsuid_core.webconsole.plugins_api import _plugin_store_order_key

    plugin_ids = [f"plugin-{index:02d}" for index in range(48)]

    def ordered(seed: str) -> list[str]:
        return sorted(
            plugin_ids,
            key=lambda pid: _plugin_store_order_key(
                pid, deprecated=False, has_update=False, installed_tab=False, order_seed=seed
            ),
        )

    first_order = ordered("page-session-a")
    assert first_order == ordered("page-session-a") and first_order != ordered("page-session-b")
    assert set(first_order[:32]).isdisjoint(first_order[32:])

    priorities = {"plain": (False, False), "update": (False, True), "deprecated": (True, False)}
    prioritized = sorted(
        priorities,
        key=lambda pid: _plugin_store_order_key(
            pid,
            deprecated=priorities[pid][0],
            has_update=priorities[pid][1],
            installed_tab=True,
            order_seed="page-session-a",
        ),
    )
    assert prioritized == ["update", "plain", "deprecated"]


def test_auth_password_roles_and_ecdh_crypto() -> None:
    from gsuid_core.webconsole.web_api import (
        session_role,
        require_admin,
        require_auth_header,
        require_admin_header,
    )
    from gsuid_core.webconsole.auth_api import (
        _PASSWORD_PREFIX_BCRYPT,
        hash_password,
        verify_password,
        _legacy_sha256_hash,
    )
    from gsuid_core.webconsole.session_store import SessionRecord

    new_hash = hash_password("s3cret!")
    assert new_hash.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("s3cret!", new_hash) and not verify_password("wrong", new_hash)

    legacy = _legacy_sha256_hash("oldpass", "aabbccdd")
    assert not legacy.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("oldpass", legacy) and not verify_password("nope", legacy)

    def _rec(role: str) -> SessionRecord:
        return {
            "user": {"id": "1", "email": "a@b.c", "name": "n", "role": role, "avatar": None},
            "email": "a@b.c",
            "created": "t",
            "expires": "t",
        }

    admin, user = _rec("admin"), _rec("user")
    assert session_role(admin) == "admin" and require_admin(admin) is admin
    with pytest.raises(HTTPException) as ei:
        require_admin(user)
    assert ei.value.status_code == 403
    for fn in (require_auth_header, require_admin_header):
        with pytest.raises(HTTPException) as missing:
            fn(None)
        assert missing.value.status_code == 401

    info = auth_keystore.public_info()
    assert info["alg"] == "x25519-aes256gcm" and {"key_id", "alg", "pubkey", "fingerprint"} <= set(info.keys())

    result = maybe_decrypt_auth_body(_frontend_encrypt({"email": "a@b.com", "password": "hunter2"}))
    assert result["email"] == "a@b.com" and result["password"] == "hunter2" and "ts" in result

    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body({"email": "x", "password": "y"})
    with pytest.raises(AuthCryptoError):
        maybe_decrypt_auth_body(_frontend_encrypt({"email": "x", "password": "y"}, tamper_ct=True))
