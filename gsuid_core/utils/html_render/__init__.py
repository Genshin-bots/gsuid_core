"""
HTML渲染工具模块

基于 htmlkit 库提供 HTML、Markdown、纯文本到图片的渲染功能。
支持自定义字体和CSS样式配置。
"""

from typing import Optional

try:
    from htmlkit import (
        md_to_pic,
        html_to_pic,
        text_to_pic,
        init_fontconfig,
    )
except ImportError as e:
    # 引导期依赖缺失提示：早于 i18n 导入，保持纯中文
    print(f"缺少 htmlkit 库，请先安装：pip install pyrenderhtml, {e}")

from gsuid_core.i18n import t
from gsuid_core.logger import logger

# fontconfig 初始化状态
_fontconfig_initialized = False


def init_html_fontconfig(
    fontconfig_path: Optional[str] = None,
    fontconfig_file: Optional[str] = None,
    fontconfig_sysroot: Optional[str] = None,
    fc_debug: Optional[str] = None,
    fc_lang: Optional[str] = None,
    fontconfig_use_mmap: Optional[str] = None,
) -> bool:
    """
    初始化 fontconfig 配置

    Args:
        fontconfig_path: 字体配置目录路径
        fontconfig_file: 字体配置文件路径
        fontconfig_sysroot: sysroot 路径
        fc_debug: debug 级别
        fc_lang: 默认语言，如 "zh_CN"
        fontconfig_use_mmap: 是否使用 mmap，如 "yes" 或 "no"

    Returns:
        初始化是否成功
    """
    global _fontconfig_initialized

    try:
        init_fontconfig(
            fontconfig_file=fontconfig_file,
            fontconfig_path=fontconfig_path,
            fontconfig_sysroot=fontconfig_sysroot,
            fc_debug=fc_debug,
            fc_lang=fc_lang or "zh_CN",
            fontconfig_use_mmap=fontconfig_use_mmap,
        )
        _fontconfig_initialized = True
        logger.info(t("🖼️ [HTMLRender] fontconfig 初始化成功"))
        return True
    except Exception as e:
        logger.exception(t("🖼️ [HTMLRender] fontconfig 初始化失败: {e}", e=e))
        return False


async def render_html_to_bytes(
    html: str,
    *,
    max_width: float = 800.0,
    dpi: float = 96.0,
    device_height: float = 600.0,
    default_font_size: float = 12.0,
    font_name: str = "sans-serif",
    allow_refit: bool = True,
    image_format: str = "png",
    jpeg_quality: int = 100,
    lang: str = "zh",
) -> bytes:
    """
    将 HTML 渲染为图片字节数据

    Args:
        html: HTML 字符串内容
        max_width: 最大宽度，默认 800.0
        dpi: 打印分辨率，默认 96.0
        device_height: 设备高度，默认 600.0
        default_font_size: 默认字体大小，默认 12.0
        font_name: 字体名称，默认 "sans-serif"
        allow_refit: 是否允许自适应，默认 True
        image_format: 图片格式，"png" 或 "jpeg"，默认 "png"
        jpeg_quality: JPEG 质量，默认 100
        lang: 语言代码，默认 "zh"

    Returns:
        PNG 或 JPEG 格式的图片字节数据
    """
    global _fontconfig_initialized

    if not _fontconfig_initialized:
        init_html_fontconfig()

    image_bytes: bytes = await html_to_pic(
        html,
        max_width=max_width,
        dpi=dpi,
        device_height=device_height,
        default_font_size=default_font_size,
        font_name=font_name,
        allow_refit=allow_refit,
        image_format=image_format,  # type: ignore
        jpeg_quality=jpeg_quality,
        lang=lang,
    )
    return image_bytes


async def render_md_to_bytes(
    md: str = "",
    *,
    md_path: str = "",
    css_path: str = "",
    max_width: int = 500,
    dpi: float = 96.0,
    allow_refit: bool = True,
    image_format: str = "png",
    jpeg_quality: int = 100,
) -> bytes:
    """
    将 Markdown 渲染为图片字节数据

    Args:
        md: Markdown 字符串内容
        md_path: Markdown 文件路径（与 md 二选一）
        css_path: CSS 文件路径
        max_width: 最大宽度，默认 500
        dpi: 打印分辨率，默认 96.0
        allow_refit: 是否允许自适应，默认 True
        image_format: 图片格式，"png" 或 "jpeg"，默认 "png"
        jpeg_quality: JPEG 质量，默认 100

    Returns:
        PNG 或 JPEG 格式的图片字节数据
    """
    global _fontconfig_initialized

    if not _fontconfig_initialized:
        init_html_fontconfig()

    image_bytes: bytes = await md_to_pic(
        md=md,
        md_path=md_path,
        css_path=css_path,
        max_width=max_width,
        dpi=dpi,
        allow_refit=allow_refit,
        image_format=image_format,  # type: ignore
        jpeg_quality=jpeg_quality,
    )
    return image_bytes


async def render_text_to_bytes(
    text: str,
    *,
    css_path: str = "",
    max_width: int = 500,
    dpi: float = 96.0,
    allow_refit: bool = True,
    image_format: str = "png",
    jpeg_quality: int = 100,
) -> bytes:
    """
    将纯文本渲染为图片字节数据

    Args:
        text: 纯文本内容
        css_path: CSS 文件路径
        max_width: 最大宽度，默认 500
        dpi: 打印分辨率，默认 96.0
        allow_refit: 是否允许自适应，默认 True
        image_format: 图片格式，"png" 或 "jpeg"，默认 "png"
        jpeg_quality: JPEG 质量，默认 100

    Returns:
        PNG 或 JPEG 格式的图片字节数据
    """
    global _fontconfig_initialized

    if not _fontconfig_initialized:
        init_html_fontconfig()

    image_bytes: bytes = await text_to_pic(
        text,
        css_path=css_path,
        max_width=max_width,
        dpi=dpi,
        allow_refit=allow_refit,
        image_format=image_format,  # type: ignore
        jpeg_quality=jpeg_quality,
    )
    return image_bytes
