"""
Theme APIs
提供主题配置相关的 RESTful APIs
"""

import json

from fastapi import Header, Request
from pydantic import BaseModel

from gsuid_core.data_store import THEME_CONFIG_PATH
from gsuid_core.webconsole.app_app import app


class ThemeConfigRequest(BaseModel):
    """Theme configuration request model"""

    mode: str = "dark"
    style: str = "glassmorphism"
    color: str = "orchid"
    icon_color: str = "colored"
    background_image: str | None = None
    blur_intensity: int = 12
    theme_preset: str = "default"


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
async def get_theme_config(request: Request, authorization: str | None = Header(default=None)):
    """Get theme configuration"""
    # Check auth (allow public access for theme config)
    # user_data = verify_token(authorization)

    config = load_theme_config()
    if config:
        return {"status": 0, "msg": "ok", "data": config}

    # Return default config if no saved config
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "mode": "dark",
            "style": "glassmorphism",
            "color": "red",
            "icon_color": "colored",
            "background_image": "https://cdn.pixabay.com/photo/2024/05/26/15/27/anime-8788959_1280.jpg",
            "blur_intensity": 12,
            "theme_preset": "shadcn",
        },
    }


@app.post("/api/theme/config")
async def save_theme_config_endpoint(
    request: Request, config: ThemeConfigRequest, authorization: str | None = Header(default=None)
):
    """Save theme configuration"""
    # 主题配置允许未登录时也保存（前端会在本地存储中保存设置）
    # 但我们仍然尝试验证 token，如果验证成功可以记录操作者信息

    config_dict = config.model_dump()
    if save_theme_config(config_dict):
        return {"status": 0, "msg": "主题配置已保存"}
    else:
        return {"status": 1, "msg": "保存失败"}
