"""
Plugin Icon API
提供插件 ICON 图片的直接访问接口
"""

from fastapi import Request
from fastapi.responses import FileResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.utils.plugins_update.api import CORE_PATH, PLUGINS_PATH

from ._api_tags import PLUGIN_ICON


@app.get("/api/plugins/icon/{plugin_name}", summary="获取插件 ICON 图片", tags=PLUGIN_ICON)
async def get_plugin_icon(request: Request, plugin_name: str):
    """
    获取指定插件的 ICON 图片

    接收插件名称，自动去除首尾下划线后查找对应的 ICON.png 文件并返回。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称（会自动去除首尾下划线）

    Returns:
        成功时返回 PNG 图片文件
        失败时返回错误信息
    """
    # gsuid_core 是框架本身，ICON 在项目根目录
    if plugin_name.lower() in ("gsuid_core", "gscore", "早柚核心"):
        core_icon = CORE_PATH / "ICON.png"
        if core_icon.exists() and core_icon.is_file():
            return FileResponse(
                path=str(core_icon),
                media_type="image/png",
                filename="gsuid_core_ICON.png",
            )

    # 依次尝试多种名称匹配方式查找 ICON.png
    candidates = [
        plugin_name,  # 原始名称（如 _ArknightsUID）
        plugin_name.strip("_"),  # 去除首尾下划线（如 _GsCore_ -> GsCore）
        plugin_name.lower(),  # 小写
        plugin_name.strip("_").lower(),  # 去除下划线 + 小写
    ]

    for name in candidates:
        if not name:
            continue
        icon_path = PLUGINS_PATH / name / "ICON.png"
        if icon_path.exists() and icon_path.is_file():
            return FileResponse(
                path=str(icon_path),
                media_type="image/png",
                filename=f"{name}_ICON.png",
            )

    return {"status": -1, "msg": f"插件 '{plugin_name}' 的 ICON 不存在"}
