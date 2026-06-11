"""
Theme APIs
提供主题配置相关的 RESTful APIs
"""

import json
from typing import Dict, Optional

from fastapi import Depends, Request
from pydantic import Field, BaseModel

from gsuid_core.data_store import THEME_CONFIG_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

# 默认主题配置（与前端文档保持一致）
DEFAULT_THEME_CONFIG: Dict = {
    "mode": "dark",
    "style": "glassmorphism",
    "color": "red",
    "icon_color": "colored",
    "background_image": None,
    "blur_intensity": 12,
    "card_opacity": 25,
    "theme_preset": "default",
    "language": "zh-CN",
}


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


def _merge_defaults(config: Optional[Dict]) -> Dict:
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


def load_theme_config() -> dict | None:
    """Load theme config from file"""
    if THEME_CONFIG_PATH.exists():
        try:
            with open(THEME_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_theme_config(config: dict) -> bool:
    """Save theme config to file"""
    try:
        THEME_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(THEME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


@app.get("/api/theme/config")
async def get_theme_config(
    request: Request,
    _: Dict = Depends(require_auth),
):
    """Get theme configuration"""
    config = load_theme_config()
    # 读取时若存储中没有 card_opacity 等新字段，返回时补默认值，
    # 避免前端拿到 undefined 触发回退逻辑。
    return {
        "status": 0,
        "msg": "ok",
        "data": _merge_defaults(config),
    }


@app.post("/api/theme/config")
async def save_theme_config_endpoint(
    request: Request,
    config: ThemeConfigRequest,
    _: Dict = Depends(require_auth),
):
    """Save theme configuration"""
    config_dict = config.model_dump()
    # Pydantic 字段校验已确保 blur_intensity(0-24) / card_opacity(0-100) 合法，
    # 此处再次夹紧以防 model_dump 后的值被绕过校验。
    if not isinstance(config_dict.get("blur_intensity"), int):
        config_dict["blur_intensity"] = DEFAULT_THEME_CONFIG["blur_intensity"]
    if not isinstance(config_dict.get("card_opacity"), int):
        config_dict["card_opacity"] = DEFAULT_THEME_CONFIG["card_opacity"]
    config_dict["blur_intensity"] = max(0, min(24, config_dict["blur_intensity"]))
    config_dict["card_opacity"] = max(0, min(100, config_dict["card_opacity"]))

    if save_theme_config(config_dict):
        return {"status": 0, "msg": "ok"}
    else:
        return {"status": 1, "msg": "保存失败"}
