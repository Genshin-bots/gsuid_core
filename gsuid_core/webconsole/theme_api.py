"""
Theme APIs
提供主题配置相关的 RESTful APIs

包含：
- 当前主题配置的读取/保存（/api/theme/config）
- 已保存主题预设的列表/保存/应用/删除（/api/theme/presets[/...]）
"""

import json
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path

from fastapi import Depends, Request, HTTPException
from pydantic import Field, BaseModel

from gsuid_core.logger import logger
from gsuid_core.data_store import THEME_CONFIG_PATH, THEME_CONFIGS_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import THEME

# 默认主题配置（与前端文档保持一致）
DEFAULT_THEME_CONFIG: Dict[str, Any] = {
    "mode": "dark",
    "style": "glassmorphism",
    "color": "red",
    "icon_color": "colored",
    "background_image": None,
    "blur_intensity": 12,
    "card_opacity": 25,
    "theme_preset": "default",
    "language": "zh-CN",
    # 侧边栏布局：floating=悬浮卡片 / docked=贴边分栏 / line=仅分割线
    "sidebar_layout": "floating",
    # 圆角强度（px，写入 CSS --radius；0=直角，24=默认 1.5rem 观感）
    "border_radius": 24,
    # UI 字号缩放（百分比，100=浏览器默认）
    "ui_scale": 100,
    # 阴影强度（百分比 0-200 → 前端 CSS --shadow-strength 0-2；0=关闭阴影）
    "shadow_intensity": 100,
    # 侧边栏默认是否收起（仅图标模式）
    "sidebar_default_collapsed": False,
}

# 主题预设文件名最大长度（按 Unicode 字符数计，而不是字节数；
# 64 个汉字/字母足够覆盖绝大多数命名场景）
_PRESET_NAME_MAX_LEN = 64
# 主题预设文件后缀固定为 .json
_PRESET_EXT = ".json"
# Windows 文件名保留字符（即便 UTF-8 编码合法，OS 也会拒绝这些字符）
_WIN_RESERVED_CHARS = set('<>:"/\\|?*')

# 内置主题预设目录（随包发布，只读）：与用户主题并行展示，用户不可 save/delete
# 内置主题（避免误删出厂预设），但可以 apply。
BUILTIN_THEMES_DIR_NAME = "themes_builtin"
BUILTIN_THEMES_PATH: Path = Path(__file__).parent / BUILTIN_THEMES_DIR_NAME


class ThemeConfigRequest(BaseModel):
    """Theme configuration request model"""

    mode: str = Field(default="dark")
    style: str = Field(default="glassmorphism")
    color: str = Field(default="red")
    icon_color: str = Field(default="colored")
    background_image: Optional[str] = None
    blur_intensity: int = Field(default=12, ge=0, le=24)
    # ★ 新增：卡片不透明度（百分比 0-100），同时作用于纯色/毛玻璃
    card_opacity: int = Field(default=25, ge=0, le=100)
    theme_preset: str = Field(default="default")
    language: str = Field(default="zh-CN")
    # 侧边栏布局：floating=悬浮卡片 / docked=贴边分栏 / line=仅分割线
    sidebar_layout: str = Field(default="floating")
    # 圆角强度（px → CSS --radius）
    border_radius: int = Field(default=24, ge=0, le=32)
    # UI 字号缩放百分比
    ui_scale: int = Field(default=100, ge=85, le=120)
    # 阴影强度百分比（0=关闭，100=默认，200=加倍）
    shadow_intensity: int = Field(default=100, ge=0, le=200)
    # 侧边栏默认收起
    sidebar_default_collapsed: bool = Field(default=False)


class ThemePresetSaveRequest(BaseModel):
    """保存主题预设请求体"""

    # Pydantic 仅做长度限制（按 Unicode 字符数），具体安全校验（路径分隔符、
    # Windows 保留字符、内置名保护等）由 ``_sanitize_preset_name`` 处理。
    name: str = Field(min_length=1, max_length=64)
    # 可选：传入自定义配置；不传则保存当前活动主题配置
    config: Optional[ThemeConfigRequest] = None
    # 是否覆盖同名预设（默认 False，避免误删用户数据）
    overwrite: bool = Field(default=False)


class ThemePresetApplyRequest(BaseModel):
    """应用主题预设请求体"""

    name: str = Field(min_length=1, max_length=64)


def _merge_defaults(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """将存储中的旧配置与当前默认配置合并，补齐缺失字段（如 card_opacity）"""
    if not isinstance(config, dict):
        return dict(DEFAULT_THEME_CONFIG)
    merged = dict(DEFAULT_THEME_CONFIG)
    for key, value in config.items():
        if value is None and key in {"background_image"}:
            merged[key] = None
        else:
            merged[key] = value
    return merged


def load_theme_config() -> Dict[str, Any] | None:
    """Load theme config from file"""
    if THEME_CONFIG_PATH.exists():
        try:
            with open(THEME_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_theme_config(config: Dict[str, Any]) -> bool:
    """Save theme config to file"""
    try:
        THEME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(THEME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def _clamp_config_dict(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """对传入字典中的整数字段做二次夹紧，防止 Pydantic 校验被绕过。"""
    if not isinstance(config_dict.get("blur_intensity"), int):
        config_dict["blur_intensity"] = DEFAULT_THEME_CONFIG["blur_intensity"]
    if not isinstance(config_dict.get("card_opacity"), int):
        config_dict["card_opacity"] = DEFAULT_THEME_CONFIG["card_opacity"]
    config_dict["blur_intensity"] = max(0, min(24, config_dict["blur_intensity"]))
    config_dict["card_opacity"] = max(0, min(100, config_dict["card_opacity"]))
    # 侧边栏布局白名单校验
    if config_dict.get("sidebar_layout") not in {"floating", "docked", "line"}:
        config_dict["sidebar_layout"] = DEFAULT_THEME_CONFIG["sidebar_layout"]
    # 圆角 / 字号缩放
    if not isinstance(config_dict.get("border_radius"), int):
        config_dict["border_radius"] = DEFAULT_THEME_CONFIG["border_radius"]
    if not isinstance(config_dict.get("ui_scale"), int):
        config_dict["ui_scale"] = DEFAULT_THEME_CONFIG["ui_scale"]
    config_dict["border_radius"] = max(0, min(32, config_dict["border_radius"]))
    config_dict["ui_scale"] = max(85, min(120, config_dict["ui_scale"]))
    # 阴影强度
    if not isinstance(config_dict.get("shadow_intensity"), int):
        config_dict["shadow_intensity"] = DEFAULT_THEME_CONFIG["shadow_intensity"]
    config_dict["shadow_intensity"] = max(0, min(200, config_dict["shadow_intensity"]))
    # 布尔字段规范化（兼容旧 JSON 里写成 0/1 的情况）
    collapsed = config_dict.get("sidebar_default_collapsed")
    if isinstance(collapsed, bool):
        pass
    elif isinstance(collapsed, (int, float)):
        config_dict["sidebar_default_collapsed"] = bool(collapsed)
    elif isinstance(collapsed, str):
        config_dict["sidebar_default_collapsed"] = collapsed.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    else:
        config_dict["sidebar_default_collapsed"] = DEFAULT_THEME_CONFIG["sidebar_default_collapsed"]
    return config_dict


def _sanitize_preset_name(raw_name: str) -> str:
    """校验并清理预设名。

    安全相关（必须）：
    - 禁止路径分隔符 `/` `\\`
    - 禁止纯 `.` / `..`
    - 禁止控制字符（含 NUL）
    - 禁止 Windows 保留字符 `< > : " | ? *`
    - 禁止以 `.` 开头（避免隐式相对路径语义 / Linux 隐藏文件歧义）
    - 禁止以 `.` 结尾（Windows 会自动剥掉尾部 `.` 造成同名冲突）
    - 写入前再次 ``Path.resolve().relative_to(base)`` 兜底校验路径

    体验相关：
    - 长度 1-64，按 Unicode 字符数计（不是字节数，所以中文名按字数算）
    - 字符集不做限制，支持中文/日文/韩文/表情符号（只要不踩到上面安全规则）
    """
    name = (raw_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="预设名称不能为空")
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="预设名称不能包含路径分隔符")
    if name in {".", ".."}:
        raise HTTPException(status_code=400, detail="非法的预设名称")
    if name.startswith("."):
        raise HTTPException(status_code=400, detail="预设名称不能以点号开头")
    if name.endswith("."):
        raise HTTPException(status_code=400, detail="预设名称不能以点号结尾")
    if any((ord(c) < 0x20) or (ord(c) == 0x7F) for c in name):
        raise HTTPException(status_code=400, detail="预设名称不能包含控制字符")
    bad_chars = sorted({c for c in name if c in _WIN_RESERVED_CHARS})
    if bad_chars:
        raise HTTPException(
            status_code=400,
            detail=f"预设名称不能包含非法字符: {' '.join(bad_chars)}",
        )
    if len(name) > _PRESET_NAME_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"预设名称长度不能超过 {_PRESET_NAME_MAX_LEN} 个字符",
        )
    return name


def _preset_path(name: str) -> Tuple[Path, str]:
    """获取用户目录下的预设文件路径，并校验未越出 THEME_CONFIGS_PATH。"""
    safe_name = _sanitize_preset_name(name)
    target = (THEME_CONFIGS_PATH / safe_name).with_suffix(_PRESET_EXT).resolve()
    base = THEME_CONFIGS_PATH.resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的预设名称")
    return target, safe_name


def _builtin_preset_path(name: str) -> Optional[Path]:
    """若 ``name`` 是内置主题名，返回其包内 JSON 路径；否则返回 None。

    校验步骤与 ``_sanitize_preset_name`` 一致：先确保名称合法（防止 .. / 路径分隔符），
    再校验文件存在，从而避免把任意相对路径当成内置主题读取。
    """
    safe_name = _sanitize_preset_name(name)
    candidate = (BUILTIN_THEMES_PATH / safe_name).with_suffix(_PRESET_EXT)
    try:
        # 防御性 resolve：包内目录固定，相对路径不可能逃逸到包外，但仍校验一次
        resolved = candidate.resolve()
        resolved.relative_to(BUILTIN_THEMES_PATH.resolve())
    except (FileNotFoundError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def list_builtin_preset_names() -> List[str]:
    """枚举包内置主题的预设名（不含扩展名），按文件名排序。"""
    if not BUILTIN_THEMES_PATH.exists() or not BUILTIN_THEMES_PATH.is_dir():
        return []
    names: List[str] = []
    for fp in BUILTIN_THEMES_PATH.glob(f"*{_PRESET_EXT}"):
        if fp.is_file():
            names.append(fp.stem)
    return sorted(names)


def _is_builtin_reserved(name: str) -> bool:
    """判断 ``name`` 是否与某个内置主题重名（重名则受保护，save/delete 拒绝）。"""
    safe_name = (name or "").strip()
    if not safe_name:
        return False
    return safe_name in list_builtin_preset_names()


def _read_preset_file(path: Path) -> Tuple[Optional[Dict[str, Any]], bool]:
    """从给定路径读取预设 JSON。返回 ``(merged_config, valid)``。

    - ``valid=False`` 时 ``merged_config`` 为 None（文件损坏）。
    - 无论是否合法，都会尽量尝试获取 stat 信息。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return _merge_defaults(payload), True
    except Exception:
        return None, False


def _describe_preset(
    name: str,
    path: Path,
    active_merged: Dict[str, Any],
    is_builtin: bool,
    merged_config: Optional[Dict[str, Any]],
    valid: bool,
) -> Dict[str, Any]:
    """构造列表接口返回的单条记录。

    ``merged_config`` 已由 ``_read_preset_file`` 合并过默认值：
    - 当 ``valid=True`` 时附带 ``config`` 字段（完整 ThemeConfig），供前端做预览图/主色；
    - 当 ``valid=False``（文件损坏）时省略 ``config``，前端按占位渐变渲染。
    """
    try:
        stat = path.stat()
    except OSError:
        stat = None
    is_active = valid and merged_config is not None and merged_config == active_merged
    record: Dict[str, Any] = {
        "name": name,
        "filename": path.name,
        # 内置主题暴露相对位置便于前端在调试面板中区分来源
        "source": "builtin" if is_builtin else "user",
        "size_bytes": stat.st_size if stat else 0,
        "mtime": stat.st_mtime if stat else 0.0,
        "is_active": is_active,
        "valid": valid,
        "is_builtin": is_builtin,
    }
    # 损坏的预设不暴露可能不完整的结构，前端走占位渐变即可。
    if valid and merged_config is not None:
        record["config"] = merged_config
    return record


def list_theme_presets() -> List[Dict[str, Any]]:
    """枚举所有主题预设（内置 + 用户），合并返回。

    返回结构：
    - name: 文件名（不含扩展名）
    - filename: 完整文件名
    - source: ``"builtin"`` / ``"user"``
    - is_builtin: 是否为内置预设
    - is_active: 是否与当前活动主题配置一致
    - valid: JSON 是否可解析
    - size_bytes / mtime: 文件元数据
    - config: 合法预设附带合并默认值后的完整 ThemeConfig，供前端预览；
      损坏预设省略，前端走占位渐变。

    排序：内置优先按内置顺序，用户按字典序。同名（理论不应出现）以用户为准。
    """
    active = load_theme_config() or {}
    active_merged = _merge_defaults(active)

    entries: Dict[str, Dict[str, Any]] = {}

    # 1) 内置主题
    for name in list_builtin_preset_names():
        builtin_path = BUILTIN_THEMES_PATH / f"{name}{_PRESET_EXT}"
        merged, valid = _read_preset_file(builtin_path)
        entries[name] = _describe_preset(
            name=name,
            path=builtin_path,
            active_merged=active_merged,
            is_builtin=True,
            merged_config=merged,
            valid=valid,
        )

    # 2) 用户主题（同名时覆盖内置的 source 标记）
    if THEME_CONFIGS_PATH.exists():
        for fp in sorted(THEME_CONFIGS_PATH.glob(f"*{_PRESET_EXT}")):
            if not fp.is_file():
                continue
            merged, valid = _read_preset_file(fp)
            entries[fp.stem] = _describe_preset(
                name=fp.stem,
                path=fp,
                active_merged=active_merged,
                is_builtin=False,
                merged_config=merged,
                valid=valid,
            )

    return list(entries.values())


def read_theme_preset(name: str) -> Dict[str, Any]:
    """读取指定名称的预设；内置优先，用户目录兜底；都不存在时抛 404。

    返回的 dict 已与默认值合并，调用方可直接写入活动配置或返回前端。
    """
    builtin_path = _builtin_preset_path(name)
    if builtin_path is not None:
        payload, valid = _read_preset_file(builtin_path)
        if not valid or payload is None:
            raise HTTPException(status_code=500, detail=f"内置主题 '{name}' 损坏")
        return payload

    target, _safe = _preset_path(name)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"主题预设 '{name}' 不存在")
    payload, valid = _read_preset_file(target)
    if not valid or payload is None:
        raise HTTPException(status_code=500, detail=f"读取预设失败: '{name}' 内容不是合法 JSON")
    return payload


@app.get("/api/theme/config", summary="获取主题配置", tags=THEME)
async def get_theme_config(
    request: Request,
):
    """Get theme configuration (公开接口，无需鉴权)

    加载界面在用户未登录时就需要读取主题配置（例如背景图/毛玻璃强度等），
    若此处要求鉴权会返回 401，前端重试导致白屏循环。
    """
    config = load_theme_config()
    # 读取时若存储中没有 card_opacity 等新字段，返回时补默认值，
    # 避免前端拿到 undefined 触发回退逻辑。
    return {
        "status": 0,
        "msg": "ok",
        "data": _merge_defaults(config),
    }


@app.post("/api/theme/config", summary="保存主题配置", tags=THEME)
async def save_theme_config_endpoint(
    request: Request,
    config: ThemeConfigRequest,
    _: Dict[str, Any] = Depends(require_auth),
):
    """Save theme configuration"""
    config_dict = config.model_dump()
    # Pydantic 字段校验已确保 blur_intensity(0-24) / card_opacity(0-100) 合法，
    # 此处再次夹紧以防 model_dump 后的值被绕过校验。
    config_dict = _clamp_config_dict(config_dict)

    if save_theme_config(config_dict):
        return {"status": 0, "msg": "ok"}
    else:
        return {"status": 1, "msg": "保存失败"}


# ==================== 主题预设（保存/列表/应用/删除） ====================


@app.get("/api/theme/presets", summary="获取主题预设列表", tags=THEME)
async def get_theme_presets(
    request: Request,
):
    """获取已保存的主题预设列表（公开接口）。

    加载主题设置面板时即调用，未登录用户也应可读。
    """
    presets = list_theme_presets()
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "user_presets_path": str(THEME_CONFIGS_PATH),
            "builtin_presets_path": str(BUILTIN_THEMES_PATH),
            "presets": presets,
        },
    }


@app.post("/api/theme/presets/save", summary="保存主题预设", tags=THEME)
async def save_theme_preset(
    request: Request,
    payload: ThemePresetSaveRequest,
    _: Dict[str, Any] = Depends(require_auth),
):
    """保存主题预设。

    - ``name``：预设名称，支持任意 Unicode 字符（含中文），1-64 字符；
      不能是内置主题名（保留）。
    - ``config``：可选；要保存的主题配置；不传则保存当前活动主题配置。
    - ``overwrite``：是否覆盖同名用户预设，默认 ``False``（防止误删）。
    """
    # 名称非法统一转 status=1 信封（与本功能其余错误返回保持一致）
    try:
        target, safe_name = _preset_path(payload.name)
    except HTTPException as e:
        return {"status": 1, "msg": str(e.detail)}

    # 内置主题名保留：禁止 save，避免覆盖包内只读资源或与内置混淆。
    # 如需基于内置主题定制，请使用新名称（如 ``"我的纯色质感"``）保存。
    if _is_builtin_reserved(safe_name):
        return {
            "status": 1,
            "msg": (f"'{safe_name}' 是内置主题名，不可被用户预设占用。请使用其他名称保存自定义版本。"),
        }

    if target.exists() and not payload.overwrite:
        return {
            "status": 1,
            "msg": f"主题预设 '{safe_name}' 已存在，如需覆盖请设置 overwrite=true",
        }

    if payload.config is not None:
        config_dict = payload.config.model_dump()
        config_dict = _clamp_config_dict(config_dict)
    else:
        # 未显式传入：保存当前活动主题配置；不存在时使用默认配置作为基底
        current = load_theme_config()
        config_dict = _merge_defaults(current)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(config_dict, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.exception(f"[Theme] 保存主题预设失败: {e}")
        return {"status": 1, "msg": f"保存失败: {e}"}

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "name": safe_name,
            "filename": target.name,
            "source": "user",
        },
    }


@app.post("/api/theme/presets/apply", summary="应用主题预设", tags=THEME)
async def apply_theme_preset(
    request: Request,
    payload: ThemePresetApplyRequest,
    _: Dict[str, Any] = Depends(require_auth),
):
    """应用主题预设：将命名预设的内容写入当前活动主题配置（theme_config.json）。"""
    # read_theme_preset 内部已校验路径与存在性；非法 / 不存在 / 损坏统一转 status=1 信封
    try:
        safe_name = _sanitize_preset_name(payload.name)
        config_dict = read_theme_preset(payload.name)
    except HTTPException as e:
        return {"status": 1, "msg": str(e.detail)}
    # 应用前再次夹紧数值字段，避免外部直接编辑预设文件导致越界
    config_dict = _clamp_config_dict(config_dict)

    if save_theme_config(config_dict):
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": safe_name,
                "config": config_dict,
            },
        }
    return {"status": 1, "msg": "应用失败"}


@app.delete("/api/theme/presets/{name}", summary="删除主题预设", tags=THEME)
async def delete_theme_preset(
    name: str,
    _: Dict[str, Any] = Depends(require_auth),
):
    """删除指定主题预设。

    内置主题名不可删除（包内资源，由 ``BUILTIN_THEMES_PATH`` 下的 JSON 文件定义）。
    """
    # 名称非法统一转 status=1 信封（与本功能其余错误返回保持一致）
    try:
        target, safe_name = _preset_path(name)
    except HTTPException as e:
        return {"status": 1, "msg": str(e.detail)}

    # 拒绝删除内置主题
    if _is_builtin_reserved(safe_name):
        return {
            "status": 1,
            "msg": f"'{safe_name}' 是内置主题，不允许删除",
        }

    if not target.exists() or not target.is_file():
        return {"status": 1, "msg": f"主题预设 '{safe_name}' 不存在"}
    try:
        target.unlink()
    except Exception as e:
        logger.exception(f"[Theme] 删除主题预设失败: {e}")
        return {"status": 1, "msg": f"删除失败: {e}"}
    return {
        "status": 0,
        "msg": "ok",
        "data": {"name": safe_name, "source": "user"},
    }
