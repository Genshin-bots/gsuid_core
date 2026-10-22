"""复制 provider 配置：直接拷贝 JSON，不经过会建空文件的 get_config。"""

from gsuid_core.utils.plugins_config.gs_config import ConfigSetManager


def _manager(tmp_path) -> ConfigSetManager:
    return ConfigSetManager(tmp_path, {})


def test_copy_keeps_secret_bytes(tmp_path) -> None:
    manager = _manager(tmp_path)
    src = tmp_path / "MiniMAX.json"
    src.write_text('{"api_key": "sk-live-secret"}', encoding="utf-8")

    assert manager.copy("MiniMAX", "MiniMAX 副本") is None
    copied = tmp_path / "MiniMAX 副本.json"
    assert copied.read_text(encoding="utf-8") == '{"api_key": "sk-live-secret"}'
    assert src.exists()


def test_copy_rejects_missing_same_name_plus_and_existing(tmp_path) -> None:
    manager = _manager(tmp_path)
    (tmp_path / "A.json").write_text("{}", encoding="utf-8")
    (tmp_path / "B.json").write_text("{}", encoding="utf-8")

    assert manager.copy("missing", "C") == "配置文件 'missing' 不存在"
    assert manager.copy("A", "A") == "新名称与原配置相同"
    assert manager.copy("A", "  ") == "名称不能为空"
    assert "不允许包含" in (manager.copy("A", "bad+name") or "")
    assert manager.copy("A", "B") == "配置文件 'B' 已存在"
    assert not (tmp_path / "bad+name.json").exists()


def test_copy_drops_stale_cache_entry(tmp_path) -> None:
    manager = _manager(tmp_path)
    (tmp_path / "A.json").write_text('{"api_key": "sk-new"}', encoding="utf-8")
    manager._cache["副本"] = object()  # type: ignore[assignment]

    assert manager.copy("A", "副本") is None
    assert "副本" not in manager._cache
