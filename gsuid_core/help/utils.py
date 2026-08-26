from typing import Optional
from pathlib import Path

from PIL import Image

ICON = Path(__file__).parent.parent.parent / "ICON.png"
plugins_help = {
    "插件帮助一览": {"desc": "这里可以看到注册过的插件帮助。", "data": []},
}


def register_help(
    name: str,
    help: str,
    icon: Optional[Image.Image] = None,
):
    if icon is None:
        icon = Image.open(ICON)
    # 帮助图把图标自身当粘贴掩码，非 RGBA 会抛 bad transparency mask
    if icon.mode != "RGBA":
        icon = icon.convert("RGBA")
    plugin_help = {
        "name": name,
        "desc": f"{name}插件帮助功能",
        "eg": f"发送 {help} 获得帮助",
        "icon": icon,
        "need_ck": False,
        "need_sk": False,
        "need_admin": False,
    }
    if plugin_help not in plugins_help["插件帮助一览"]["data"]:
        plugins_help["插件帮助一览"]["data"].append(plugin_help)


def clean_plugin_help(plugin_name: str) -> None:
    """清理指定插件的帮助缓存与一览条目，并使 GsCore 主帮助图失效。"""
    from gsuid_core.data_store import get_res_path
    from gsuid_core.help.draw_new_plugin_help import cache as new_cache
    from gsuid_core.help.draw_plugin_help import cache as old_cache

    # 清理内存 cache 标记（含自身与 GsCore 主帮助）
    new_cache.pop(plugin_name, None)
    new_cache.pop("GsCore", None)
    old_cache.pop(plugin_name, None)
    old_cache.pop("GsCore", None)

    # 剔除一览表中的注册条目
    category = plugins_help.get("插件帮助一览")
    if category is not None:
        category["data"] = [item for item in category["data"] if item["name"] != plugin_name]

    # 删除磁盘上的过期帮助图
    help_dir = get_res_path("help")
    for pattern in (f"{plugin_name}_*.jpg", f"{plugin_name}.jpg", "GsCore_*.jpg", "GsCore.jpg"):
        for file in help_dir.glob(pattern):
            if file.is_file():
                file.unlink(missing_ok=True)
