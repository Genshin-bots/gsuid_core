"""文件系统路径围栏：拒绝穿越、绝对路径换根、Windows 盘符/保留名。

所有把用户输入拼进 ``Path`` / ``open`` / ``FileResponse`` / ``unlink`` / ``rmtree``
的 webconsole（及下层人格/插件路径）必须走这里，不要自己 ``root / user``。
"""

from __future__ import annotations

import os
import re
from typing import Iterable
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse


class PathEscapeError(ValueError):
    """用户路径越出允许的根目录，或文件名本身非法。"""


_WIN_RESERVED_CHARS = set('<>:"|?*')
_WIN_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})
_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "100.100.100.200",
    }
)


def _has_ctrl(name: str) -> bool:
    return any((ord(c) < 0x20) or (ord(c) == 0x7F) for c in name)


def _is_win_reserved_part(part: str) -> bool:
    stem = part.split(".", 1)[0].upper()
    return stem in _WIN_RESERVED_NAMES


def is_safe_relpath(rel: str) -> bool:
    """相对路径是否可拼到根下：禁止绝对路径、``.`` / ``..`` 段、分隔盘符、控制字符。

    允许多段（``assets/js/a.js``）和 Unicode 文件名（``下载.jpg``）。
    ``foo..jpg`` 这类「子串含 .. 但不是独立段」合法。
    """
    if not isinstance(rel, str):
        return False
    name = rel.strip()
    if not name or name != rel:
        return False
    if _has_ctrl(name):
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        return False
    if len(normalized) >= 2 and normalized[1] == ":":
        return False
    if any(c in name for c in _WIN_RESERVED_CHARS):
        return False
    parts = Path(normalized).parts
    if not parts:
        return False
    for part in parts:
        if part in {"", ".", ".."}:
            return False
        if _has_ctrl(part) or _is_win_reserved_part(part):
            return False
    return True


def is_safe_filename(name: str) -> bool:
    """单段文件名（不含任何路径分隔）。"""
    if not is_safe_relpath(name):
        return False
    return "/" not in name.replace("\\", "/")


def is_under_root(path: Path, root: Path) -> bool:
    """``path`` 解析后是否落在 ``root`` 子树（含自身）。Windows 上按 normcase 比较。"""
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(base)
        return True
    except ValueError:
        pass
    if os.name == "nt":
        r = os.path.normcase(str(resolved))
        b = os.path.normcase(str(base))
        return r == b or r.startswith(b + os.sep)
    return False


def safe_join(root: Path, *parts: str) -> Path:
    """把相对段拼到 ``root`` 下并 ``resolve``；越界或非法段抛 ``PathEscapeError``。"""
    if not parts:
        raise PathEscapeError("路径为空")
    try:
        root_res = root.resolve()
    except OSError as e:
        raise PathEscapeError(f"根目录不可用: {e}") from e
    rel = "/".join(str(p).replace("\\", "/") for p in parts if p is not None and str(p) != "")
    if not rel:
        raise PathEscapeError("路径为空")
    if not is_safe_relpath(rel):
        raise PathEscapeError("非法路径")
    candidate = root_res.joinpath(*Path(rel.replace("\\", "/")).parts)
    try:
        resolved = candidate.resolve()
    except OSError as e:
        raise PathEscapeError(f"路径不可用: {e}") from e
    if not is_under_root(resolved, root_res):
        raise PathEscapeError("越界路径")
    return resolved


def confine_to_root(user_path: str, root: Path) -> Path:
    """允许相对路径或已经落在 ``root`` 内的绝对路径；其它一律拒绝。"""
    raw = (user_path or "").strip()
    if not raw:
        raise PathEscapeError("路径为空")
    p = Path(raw)
    try:
        root_res = root.resolve()
    except OSError as e:
        raise PathEscapeError(f"根目录不可用: {e}") from e
    if p.is_absolute() or p.drive:
        try:
            resolved = p.resolve()
        except OSError as e:
            raise PathEscapeError(f"路径不可用: {e}") from e
        if not is_under_root(resolved, root_res):
            raise PathEscapeError("越界路径")
        return resolved
    return safe_join(root_res, raw)


def resolve_under_root(raw: str, root: Path, *, aliases: frozenset[str] = frozenset({"."})) -> Path:
    """相对名若在 ``aliases`` 中则落到 ``root`` 自身，否则 ``confine_to_root``。"""
    text = raw.strip().rstrip("/\\")
    if text in aliases:
        try:
            return root.resolve()
        except OSError as e:
            raise PathEscapeError(f"根目录不可用: {e}") from e
    return confine_to_root(raw.strip(), root)


def ensure_under_any(path: Path, roots: Iterable[Path]) -> Path:
    """已有路径必须落在任一允许根下。"""
    try:
        resolved = path.resolve()
    except OSError as e:
        raise PathEscapeError(f"路径不可用: {e}") from e
    for root in roots:
        if is_under_root(resolved, root):
            return resolved
    raise PathEscapeError("越界路径")


def parse_iso_date(raw: str | None, *, default_today: bool = True) -> str:
    """只接受 ``YYYY-MM-DD``（可带一次 ``.log`` 后缀）。非法则抛 ``PathEscapeError``。"""
    if raw is None or not str(raw).strip():
        if default_today:
            return datetime.now().strftime("%Y-%m-%d")
        raise PathEscapeError("日期为空")
    s = str(raw).strip()
    if s.endswith(".log"):
        s = s[: -len(".log")]
    if not _ISO_DATE_RE.match(s):
        raise PathEscapeError("非法日期")
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise PathEscapeError("非法日期") from e
    return s


def validate_install_source_url(url: str) -> str | None:
    """技能/插件安装源：只允许 http(s)/ssh/git 与 ``git@host:path``。

    拒绝 ``file://``、本机回环、链路本地与云元数据主机。
    内网 RFC1918 不拦（自建 GitLab 常见），写入端须配合管理员鉴权。
    返回 None 表示通过，否则是给前端的错误文案。
    """
    text = (url or "").strip()
    if not text:
        return "请提供有效的来源地址"
    if text.startswith("git@"):
        host = text[4:].split(":", 1)[0].split("/")[0].lower()
        if host in _LOOPBACK_HOSTS or host in _METADATA_HOSTS:
            return "不允许指向本机或元数据地址的来源"
        return None
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https", "ssh", "git"}:
        return "URL 协议不支持，仅允许 http://、https://、ssh://、git@ 开头的地址"
    host = (parsed.hostname or "").lower()
    if not host:
        return "来源地址缺少主机名"
    if host in _LOOPBACK_HOSTS or host in _METADATA_HOSTS:
        return "不允许指向本机或元数据地址的来源"
    if host.startswith("169.254.") or host.startswith("fe80:") or host.startswith("::ffff:127."):
        return "不允许指向链路本地地址的来源"
    return None
