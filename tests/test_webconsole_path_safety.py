"""webconsole 路径穿越 / 密钥掩码 / 密码哈希 回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    parse_iso_date,
    confine_to_root,
    is_safe_relpath,
    is_safe_filename,
    resolve_under_root,
    validate_install_source_url,
)
from gsuid_core.utils.secret_mask import (
    looks_masked,
    mask_mapping,
    unmask_against,
    mask_secret_value,
    is_secret_key_name,
)


def test_safe_join_blocks_dotdot(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("ok", encoding="utf-8")
    secret = tmp_path / "config.json"
    secret.write_text("secret", encoding="utf-8")

    with pytest.raises(PathEscapeError):
        safe_join(root, "../../../config.json")
    with pytest.raises(PathEscapeError):
        safe_join(root, "..", "..", "config.json")
    with pytest.raises(PathEscapeError):
        safe_join(root, "..\\..\\config.json")

    inside = safe_join(root, "index.html")
    assert inside.is_file()
    assert inside.read_text(encoding="utf-8") == "ok"


def test_safe_join_blocks_absolute_and_drive(tmp_path: Path):
    root = tmp_path / "dist"
    root.mkdir()
    with pytest.raises(PathEscapeError):
        safe_join(root, "C:/Windows/win.ini")
    with pytest.raises(PathEscapeError):
        safe_join(root, "/etc/passwd")


def test_safe_join_allows_nested_and_unicode(tmp_path: Path):
    root = tmp_path / "dist"
    nested = root / "assets" / "js"
    nested.mkdir(parents=True)
    target = nested / "index.js"
    target.write_text("1", encoding="utf-8")
    cn = root / "下载.jpg"
    cn.write_bytes(b"x")

    assert safe_join(root, "assets/js/index.js") == target.resolve()
    assert safe_join(root, "下载.jpg") == cn.resolve()
    # 子串含 .. 但不是路径段
    weird = root / "foo..jpg"
    weird.write_bytes(b"y")
    assert safe_join(root, "foo..jpg") == weird.resolve()


def test_confine_to_root_allows_inside_absolute(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    f = root / "a.png"
    f.write_bytes(b"1")
    assert confine_to_root(str(f), root) == f.resolve()
    with pytest.raises(PathEscapeError):
        confine_to_root(str(tmp_path / "outside.txt"), root)


def test_is_safe_filename_rejects_separators():
    assert is_safe_filename("avatar.png")
    assert not is_safe_filename("../config.json")
    assert not is_safe_filename("a/b.png")
    assert not is_safe_filename("..")
    assert not is_safe_relpath("assets/../../../data/config.json")


def test_parse_iso_date():
    assert parse_iso_date("2026-08-16") == "2026-08-16"
    assert parse_iso_date("2026-08-16.log") == "2026-08-16"
    with pytest.raises(PathEscapeError):
        parse_iso_date("../../config", default_today=False)
    with pytest.raises(PathEscapeError):
        parse_iso_date("2026-13-40", default_today=False)


def test_validate_install_source_url():
    assert validate_install_source_url("https://github.com/owner/repo.git") is None
    assert validate_install_source_url("git@github.com:owner/repo.git") is None
    assert validate_install_source_url("file:///etc/passwd") is not None
    assert validate_install_source_url("http://127.0.0.1/x") is not None
    assert validate_install_source_url("http://169.254.169.254/latest") is not None
    assert validate_install_source_url("http://100.100.100.200/latest") is not None
    assert validate_install_source_url("http://[fe80::1]/x") is not None


def test_mask_and_unmask_roundtrip():
    raw = {"api_key": {"title": "k", "data": "sk-abcdefgh", "secret": True}, "model": "gpt"}
    masked = mask_mapping(raw)
    assert looks_masked(masked["api_key"]["data"])
    assert masked["api_key"]["data"] != "sk-abcdefgh"
    restored = unmask_against(masked, raw)
    assert restored["api_key"]["data"] == "sk-abcdefgh"
    assert mask_secret_value("ab") == "****"


def test_mask_config_item_without_secret_flag():
    item = {"title": "k", "data": "sk-abcdefgh", "options": []}
    masked = mask_secret_value(item)
    assert looks_masked(masked["data"])
    assert masked["title"] == "k"


def test_unmask_list_keeps_new_keys():
    old = ["sk-oldkey123"]
    incoming = [mask_secret_value("sk-oldkey123"), "sk-newkey456"]
    merged = unmask_against(incoming, old)
    assert merged[0] == "sk-oldkey123"
    assert merged[1] == "sk-newkey456"


def test_looks_masked_nested_dict():
    assert looks_masked({"a": {"b": "ab****cd"}})


def test_secret_key_name_includes_salt():
    assert is_secret_key_name("end_user_id_salt")
    assert is_secret_key_name("webdav_password")
    assert is_secret_key_name("WS_TOKEN")
    assert not is_secret_key_name("embedding_model")


def test_mask_mapping_salt_field():
    raw = {
        "end_user_id_salt": {"title": "盐", "data": "random-salt-value", "secret": True},
        "model": {"title": "m", "data": "gpt"},
    }
    masked = mask_mapping(raw)
    assert looks_masked(masked["end_user_id_salt"]["data"])
    assert masked["model"]["data"] == "gpt"
    assert unmask_against(masked, raw)["end_user_id_salt"]["data"] == "random-salt-value"


def test_is_safe_relpath_rejects_whitespace():
    assert not is_safe_relpath(" foo.txt")
    assert not is_safe_relpath("foo.txt ")


def test_asset_data_alias_is_data_root():
    from gsuid_core.data_store import gs_data_path

    aliases = frozenset({".", "data"})
    assert resolve_under_root("data", gs_data_path, aliases=aliases) == gs_data_path.resolve()
    assert resolve_under_root("data/", gs_data_path, aliases=aliases) == gs_data_path.resolve()


def test_password_bcrypt_and_legacy_upgrade():
    from gsuid_core.webconsole.auth_api import (
        _PASSWORD_PREFIX_BCRYPT,
        hash_password,
        verify_password,
        _legacy_sha256_hash,
    )

    new_hash = hash_password("s3cret!")
    assert new_hash.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("s3cret!", new_hash)
    assert not verify_password("wrong", new_hash)

    legacy = _legacy_sha256_hash("oldpass", "aabbccdd")
    assert not legacy.startswith(_PASSWORD_PREFIX_BCRYPT)
    assert verify_password("oldpass", legacy)
    assert not verify_password("nope", legacy)


def test_resolve_plugin_path_rejects_dotdot():
    from gsuid_core.utils.plugins_update.git_update import _resolve_plugin_path

    assert _resolve_plugin_path("..") is None
    assert _resolve_plugin_path("../..") is None
    assert _resolve_plugin_path("..%2F..") is None


def test_meme_folder_rejects_traversal():
    from gsuid_core.ai_core.meme.library import get_folder_path

    with pytest.raises(PathEscapeError):
        get_folder_path("..")
    with pytest.raises(PathEscapeError):
        get_folder_path("../../data")


def test_require_admin_role():
    from fastapi import HTTPException

    from gsuid_core.webconsole.web_api import session_role, require_admin
    from gsuid_core.webconsole.session_store import SessionRecord

    def _rec(role: str) -> SessionRecord:
        return {
            "user": {"id": "1", "email": "a@b.c", "name": "n", "role": role, "avatar": None},
            "email": "a@b.c",
            "created": "t",
            "expires": "t",
        }

    admin = _rec("admin")
    user = _rec("user")
    assert session_role(admin) == "admin"
    assert session_role(user) == "user"
    assert require_admin(admin) is admin
    with pytest.raises(HTTPException) as ei:
        require_admin(user)
    assert ei.value.status_code == 403


def test_livechat_ws_authorized_requires_session():
    from gsuid_core.webconsole.web_api import livechat_ws_authorized

    assert livechat_ws_authorized(None) is False
    assert livechat_ws_authorized("") is False
    assert livechat_ws_authorized("not-a-session") is False
