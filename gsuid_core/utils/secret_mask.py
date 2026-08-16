"""配置回传脱敏：GET 掩码，POST 遇到掩码值则保留旧值，避免保存时把 **** 写回。"""

from __future__ import annotations

from typing import Any, Mapping

_SECRET_EXACT = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "password",
        "token",
        "ws_token",
        "register_code",
        "access_token",
        "secret_key",
        "private_key",
        "client_secret",
        "refresh_token",
        "authorization",
    }
)
_SECRET_SUFFIXES = ("_key", "_secret", "_token", "_password", "_passwd", "_salt")
_MASK = "****"


def is_secret_key_name(name: str) -> bool:
    n = str(name).strip().lower().replace("-", "_")
    if n in _SECRET_EXACT:
        return True
    return any(n.endswith(suf) for suf in _SECRET_SUFFIXES)


def looks_masked(value: Any) -> bool:
    if isinstance(value, str):
        return _MASK in value
    if isinstance(value, list):
        return any(looks_masked(v) for v in value)
    if isinstance(value, dict):
        return any(looks_masked(v) for v in value.values())
    return False


def mask_secret_value(value: Any) -> Any:
    if value is None or value == "":
        return value
    if isinstance(value, list):
        return [mask_secret_value(v) for v in value]
    if isinstance(value, str):
        if len(value) <= 4:
            return _MASK
        return f"{value[:2]}{_MASK}{value[-2:]}"
    if isinstance(value, dict):
        # GsConfig 项是 {title, data, secret}：父键已是 api_key 时仍要掩 data
        if "data" in value:
            out: dict[str, Any] = {}
            for key, item in value.items():
                out[key] = mask_secret_value(item) if key == "data" else item
            return out
        return mask_mapping(value)
    return _MASK


def mask_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    """按键名脱敏；``secret: True`` 的配置项同时掩 ``data``。"""
    out: dict[str, Any] = {}
    parent_secret = "secret" in data and data["secret"] is True
    for key, value in data.items():
        if is_secret_key_name(str(key)) or (parent_secret and key == "data"):
            out[key] = mask_secret_value(value)
        elif isinstance(value, dict):
            out[key] = mask_mapping(value)
        elif isinstance(value, list):
            out[key] = [mask_mapping(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def unmask_against(new_value: Any, old_value: Any) -> Any:
    """新值若是掩码则保留旧值。列表按元素合并，以便 tags 密钥追加新项。"""
    if isinstance(new_value, dict) and isinstance(old_value, dict):
        merged: dict[str, Any] = {}
        for key, val in new_value.items():
            old_item = old_value[key] if key in old_value else None
            merged[key] = unmask_against(val, old_item)
        return merged
    if isinstance(new_value, list) and isinstance(old_value, list):
        merged_list: list[Any] = []
        for i, n in enumerate(new_value):
            old_item = old_value[i] if i < len(old_value) else None
            merged_list.append(unmask_against(n, old_item))
        return merged_list
    if looks_masked(new_value) and old_value is not None:
        return old_value
    return new_value
