"""
HTML渲染工具模块

基于 pytakumi 库提供 HTML、Markdown、纯文本到图片的渲染功能。
对外保持 ``render_*_to_bytes`` 异步接口，内部用共享 Renderer 做字体/缓存复用。
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger

try:
    from pytakumi import (
        Renderer,
        md_to_pic,
        html_to_pic,
        text_to_pic,
        set_glyph_cache_max_bytes,
    )

    _PYTAKUMI_AVAILABLE = True
except ImportError as e:  # pragma: no cover - 引导期依赖缺失
    _PYTAKUMI_AVAILABLE = False
    _IMPORT_ERROR = e
    # 引导期依赖缺失提示：早于 i18n 导入，保持纯中文
    print(f"缺少 pytakumi 库，请先安装：pip install pytakumi, {e}")

    def _missing_dependency(name: str):
        def _raise(*args: object, **kwargs: object) -> object:
            raise RuntimeError(f"html_render.{name} 调用失败: pytakumi 未安装。请先安装: pip install pytakumi")

        return _raise

    html_to_pic = _missing_dependency("html_to_pic")  # type: ignore[assignment]
    md_to_pic = _missing_dependency("md_to_pic")  # type: ignore[assignment]
    text_to_pic = _missing_dependency("text_to_pic")  # type: ignore[assignment]
    Renderer = None  # type: ignore[assignment,misc]
    set_glyph_cache_max_bytes = _missing_dependency("set_glyph_cache_max_bytes")  # type: ignore[assignment]

# 框架内置中文字体（与 PIL core_font 同源）
_FONT_PATH = Path(__file__).resolve().parent.parent / "fonts" / "MiSans-Bold.ttf"
_DEFAULT_FONT_NAME = "MiSans"
_MONO_FONT_NAME = "Mono"
_GLYPH_CACHE_BYTES = 64 * 1024 * 1024

_renderer: Any = None
_renderer_ready = False


def _find_mono_font() -> Optional[bytes]:
    """尽力查找一个等宽字体用于代码渲染（找不到返回 None，优雅降级）。

    pytakumi 只使用已注册的字体，系统字体不会自动生效，因此代码块若想要
    真正的等宽效果，必须显式注册一个等宽字体。这里按「项目内置优先、
    常见系统字体兜底」的顺序查找，跨 Windows / macOS / Linux。
    """
    fonts_dir = Path(__file__).resolve().parent.parent / "fonts"
    candidates: list[Path] = []

    # 1) 项目内置（任何 *mono* 命名的 ttf/otf）
    if fonts_dir.is_dir():
        candidates.extend(sorted(fonts_dir.glob("*[Mm]ono*.ttf")))
        candidates.extend(sorted(fonts_dir.glob("*[Mm]ono*.otf")))

    # 2) 常见系统等宽字体
    home = Path.home()
    candidates += [
        # Windows
        Path(r"C:\Windows\Fonts\consola.ttf"),  # Consolas
        Path(r"C:\Windows\Fonts\cascmono.ttf"),  # Cascadia Mono
        # macOS
        Path("/System/Library/Fonts/Menlo.ttc"),
        Path("/System/Library/Fonts/Monaco.ttf"),
        # Linux
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"),
        Path("/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf"),
        home / ".local/share/fonts/DejaVuSansMono.ttf",
    ]

    for path in candidates:
        try:
            if path.is_file():
                return path.read_bytes()
        except OSError:
            continue
    return None


def _ensure_renderer(
    *,
    extra_fonts: Optional[list[tuple[bytes, str]]] = None,
    force: bool = False,
) -> Any:
    """懒加载共享 Renderer，注册 MiSans 等 CJK 字体。"""
    global _renderer, _renderer_ready

    if not _PYTAKUMI_AVAILABLE:
        raise RuntimeError("html_render 调用失败: pytakumi 未安装。请先安装: pip install pytakumi")

    if _renderer_ready and _renderer is not None and not force:
        return _renderer

    try:
        set_glyph_cache_max_bytes(_GLYPH_CACHE_BYTES)
    except Exception:
        pass

    r = Renderer(cache_max_bytes=_GLYPH_CACHE_BYTES)
    registered: list[str] = []

    if _FONT_PATH.is_file():
        try:
            r.register_font(_FONT_PATH.read_bytes(), name=_DEFAULT_FONT_NAME)
            registered.append(_DEFAULT_FONT_NAME)
        except Exception as e:
            logger.exception(t("log.htmlrender.font_register_failed", e=e))

    # 等宽字体（代码块渲染用）；找不到就跳过，代码回退到 MiSans
    mono_data = _find_mono_font()
    if mono_data is not None:
        try:
            r.register_font(mono_data, name=_MONO_FONT_NAME)
            registered.append(_MONO_FONT_NAME)
        except Exception as e:
            logger.exception(t("log.htmlrender.font_register_failed", e=e))

    if extra_fonts:
        for data, name in extra_fonts:
            try:
                r.register_font(data, name=name)
                registered.append(name)
            except Exception as e:
                logger.exception(t("log.htmlrender.font_register_failed", e=e))

    _renderer = r
    _renderer_ready = True
    logger.info(
        t(
            "log.htmlrender.renderer_initialized",
            fonts=", ".join(registered) if registered else "(none)",
        )
    )
    return _renderer


def init_html_fontconfig(
    fontconfig_path: Optional[str] = None,
    fontconfig_file: Optional[str] = None,
    fontconfig_sysroot: Optional[str] = None,
    fc_debug: Optional[str] = None,
    fc_lang: Optional[str] = None,
    fontconfig_use_mmap: Optional[str] = None,
    **_kwargs: Any,
) -> bool:
    """兼容旧接口：初始化共享渲染器与内置字体。

    pytakumi 不再使用 fontconfig，参数保留仅为 API 兼容，均被忽略。
    """
    # 旧参数仅为兼容占位
    _ = (
        fontconfig_path,
        fontconfig_file,
        fontconfig_sysroot,
        fc_debug,
        fc_lang,
        fontconfig_use_mmap,
    )
    try:
        _ensure_renderer(force=True)
        return True
    except Exception as e:
        logger.exception(t("log.htmlrender.renderer_initialization", e=e))
        return False


def _resolve_format(image_format: str) -> str:
    fmt = (image_format or "png").lower()
    if fmt in {"jpg", "jpeg"}:
        return "jpeg"
    return fmt


def _dpr_from_dpi(dpi: float | None) -> float | None:
    if dpi is None:
        return None
    try:
        dpr = float(dpi) / 96.0
    except (TypeError, ValueError):
        return None
    if abs(dpr - 1.0) < 1e-6:
        return None
    return dpr


def _font_families(font_name: str | None) -> list[str]:
    """默认带上 MiSans，保证中文渲染。"""
    names: list[str] = []
    if font_name and font_name not in {"sans-serif", "serif", "monospace"}:
        names.append(font_name)
    if _DEFAULT_FONT_NAME not in names:
        names.append(_DEFAULT_FONT_NAME)
    return names


def _sync_render_html(
    html: str,
    *,
    max_width: float,
    dpi: float,
    device_height: float,
    default_font_size: float,
    font_name: str,
    allow_refit: bool,
    image_format: str,
    jpeg_quality: int,
    lang: str,
) -> bytes:
    renderer = _ensure_renderer()
    width = max(1, int(max_width))
    height: int | None
    if allow_refit:
        height = None
    else:
        height = max(1, int(device_height))

    # 给片段补一点默认字号，避免裸 HTML 字太小
    css = f"body{{font-size:{float(default_font_size)}px;}}" if default_font_size else None
    fmt = _resolve_format(image_format)
    quality = int(jpeg_quality) if fmt == "jpeg" else None

    return html_to_pic(
        html,
        width=width,
        height=height,
        format=fmt,
        quality=quality,
        css=css,
        renderer=renderer,
        device_pixel_ratio=_dpr_from_dpi(dpi),
        font_families=_font_families(font_name),
        lang=lang or None,
    )


def _sync_render_md(
    md: str,
    *,
    css: str | None,
    max_width: int,
    dpi: float,
    allow_refit: bool,
    image_format: str,
    jpeg_quality: int,
    dark: bool = False,
) -> bytes:
    renderer = _ensure_renderer()
    width = max(1, int(max_width))
    fmt = _resolve_format(image_format)
    quality = int(jpeg_quality) if fmt == "jpeg" else None

    # pytakumi 高度默认按内容自适应；allow_refit 仅保留兼容语义
    _ = allow_refit
    return md_to_pic(
        md,
        width=width,
        height=None,
        format=fmt,
        quality=quality,
        dark=dark,
        css=css,
        renderer=renderer,
        device_pixel_ratio=_dpr_from_dpi(dpi),
        font_families=_font_families(None),
        lang="zh",
    )


def _sync_render_text(
    text: str,
    *,
    css: str | None,
    max_width: int,
    dpi: float,
    allow_refit: bool,
    image_format: str,
    jpeg_quality: int,
) -> bytes:
    renderer = _ensure_renderer()
    width = max(1, int(max_width))
    fmt = _resolve_format(image_format)
    quality = int(jpeg_quality) if fmt == "jpeg" else None

    # allow_refit 时让卡片高度随内容；否则给一个固定下限
    height: int | None = None if allow_refit else max(360, 200 + text.count("\n") * 28)

    return text_to_pic(
        text,
        width=width,
        height=height,
        format=fmt,
        quality=quality,
        css=css,
        renderer=renderer,
        device_pixel_ratio=_dpr_from_dpi(dpi),
        font_families=_font_families(None),
        lang="zh",
    )


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
        dpi: 打印分辨率，默认 96.0（映射为 device_pixel_ratio = dpi/96）
        device_height: 设备高度，默认 600.0（allow_refit=False 时生效）
        default_font_size: 默认字体大小，默认 12.0
        font_name: 字体名称，默认 "sans-serif"（会额外兜底 MiSans）
        allow_refit: 是否允许按内容自适应高度，默认 True
        image_format: 图片格式，"png" 或 "jpeg"，默认 "png"
        jpeg_quality: JPEG 质量，默认 100
        lang: 语言代码，默认 "zh"

    Returns:
        PNG 或 JPEG 格式的图片字节数据
    """
    return await asyncio.to_thread(
        _sync_render_html,
        html,
        max_width=max_width,
        dpi=dpi,
        device_height=device_height,
        default_font_size=default_font_size,
        font_name=font_name,
        allow_refit=allow_refit,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
        lang=lang,
    )


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
    dark: bool = False,
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
        dark: 是否使用暗色基底主题，默认 False

    Returns:
        PNG 或 JPEG 格式的图片字节数据
    """
    content = md
    if not content and md_path:
        content = await asyncio.to_thread(Path(md_path).read_text, encoding="utf-8")

    css: str | None = None
    if css_path:
        css = await asyncio.to_thread(Path(css_path).read_text, encoding="utf-8")

    return await asyncio.to_thread(
        _sync_render_md,
        content,
        css=css,
        max_width=max_width,
        dpi=dpi,
        allow_refit=allow_refit,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
        dark=dark,
    )


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
    css: str | None = None
    if css_path:
        css = await asyncio.to_thread(Path(css_path).read_text, encoding="utf-8")

    return await asyncio.to_thread(
        _sync_render_text,
        text,
        css=css,
        max_width=max_width,
        dpi=dpi,
        allow_refit=allow_refit,
        image_format=image_format,
        jpeg_quality=jpeg_quality,
    )
