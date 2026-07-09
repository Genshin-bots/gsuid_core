"""
Brand APIs
品牌信息（ICON、标题、副标题）相关的 RESTful APIs

端点一览：
- GET    /api/brand         读取品牌信息（公开，未登录前端可调用）
- POST   /api/brand         更新 title / subtitle（需要鉴权，JSON body）
- POST   /api/brand/icon    上传 ICON 图片（需要鉴权，multipart，PNG，≤ 2MB）
- DELETE /api/brand/icon    删除用户上传的 ICON，回退到默认 CORE_PATH/ICON.png（需要鉴权）
- GET    /api/brand/icon    获取当前品牌 ICON：用户上传优先，回退到 CORE_PATH/ICON.png（公开）

设计要点：
- 读取 / ICON 提供均为公开，与 theme_api.py 保持一致（登录页加载就需要 brand）。
- 配置文件每次实时读取，不缓存，确保 POST 后立即生效。
- 写入使用 boltons.fileutils.atomic_save，避免并发/断电导致配置损坏。
- ICON 文件直接覆盖写入，浏览器 GET 可立即看到新图。
- ICON 限制：仅 PNG，最大 2MB（与现有 meme_api.py / auth_api.py 上传约束一致）。
"""

import json
from typing import Any, Dict
from pathlib import Path

from fastapi import File, Depends, Request, UploadFile
from pydantic import Field, BaseModel
from boltons.fileutils import atomic_save
from fastapi.responses import FileResponse

from gsuid_core.logger import logger
from gsuid_core.data_store import BRAND_ICON_PATH, BRAND_CONFIG_PATH
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.plugins_update.api import CORE_PATH

from ._api_tags import BRAND

# ============================================================
# 默认品牌信息
# ============================================================

DEFAULT_BRAND: Dict[str, Any] = {
    "icon": "ICON.png",  # 仅作展示用途，默认 ICON 实际由 CORE_PATH/ICON.png 提供
    "title": "GsHub",
    "subtitle": "早柚核心",
}

# 仅允许 image/png（前端通过 <input type="file" accept="image/png"> 也可提前过滤）
_ALLOWED_ICON_MIME = {"image/png"}
# ICON 大小上限 2MB
_MAX_ICON_SIZE = 2 * 1024 * 1024

# 标题最长 64 字符、副标题最长 128 字符（足够覆盖绝大多数命名场景）
_TITLE_MAX_LEN = 64
_SUBTITLE_MAX_LEN = 128


# ============================================================
# Pydantic 模型
# ============================================================


class BrandUpdateRequest(BaseModel):
    """品牌信息更新请求体（仅 title / subtitle，ICON 走单独的上传接口）"""

    title: str = Field(
        default=DEFAULT_BRAND["title"],
        min_length=0,
        max_length=_TITLE_MAX_LEN,
        description="品牌标题（≤64 字符）",
    )
    subtitle: str = Field(
        default=DEFAULT_BRAND["subtitle"],
        min_length=0,
        max_length=_SUBTITLE_MAX_LEN,
        description="品牌副标题（≤128 字符）",
    )


# ============================================================
# 内部辅助
# ============================================================


def _read_brand_config() -> Dict[str, Any]:
    """读取 brand.json（每次实时读取，无缓存，确保即时生效）。

    与 DEFAULT_BRAND 合并，缺失字段自动补默认值，避免老配置或损坏配置
    导致前端拿到 undefined 触发回退逻辑。
    """
    if not BRAND_CONFIG_PATH.exists():
        return dict(DEFAULT_BRAND)
    try:
        with open(BRAND_CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # 配置损坏不阻断启动，回退到默认配置
        logger.warning(f"[Brand] 读取 brand.json 失败，使用默认配置: {e}")
        return dict(DEFAULT_BRAND)
    except Exception as e:
        # 保底：非预期异常也不能让读取崩掉，回退默认
        logger.exception(f"[Brand] 读取 brand.json 未知错误，使用默认配置: {e}")
        return dict(DEFAULT_BRAND)

    merged: Dict[str, Any] = dict(DEFAULT_BRAND)
    if isinstance(stored, dict):
        # 仅提取字符串字段，防御老/损坏配置混入非预期类型
        for key in ("title", "subtitle"):
            value = stored[key] if key in stored else None
            if isinstance(value, str):
                merged[key] = value
    return merged


def _write_brand_config(data: Dict[str, Any]) -> bool:
    """写入 brand.json（atomic_save 保证断电/并发下不损坏）。"""
    try:
        BRAND_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with atomic_save(
            str(BRAND_CONFIG_PATH),
            text_mode=False,
            overwrite=True,
            file_perms=0o644,
        ) as file:
            if not file:
                raise RuntimeError("写入 brand.json 失败: atomic_save 返回 None")
            json_str = json.dumps(data, indent=2, ensure_ascii=False)
            file.write(json_str.encode("utf-8"))
        return True
    except (OSError, RuntimeError) as e:
        logger.warning(f"[Brand] 写入 brand.json 失败: {e}")
        return False
    except Exception as e:
        # 保底：序列化/权限等非预期异常也不能让请求 500
        logger.exception(f"[Brand] 写入 brand.json 未知错误: {e}")
        return False


def _resolve_icon_info() -> Dict[str, str]:
    """返回 ICON URL 与来源标记。

    - 用户上传的 ICON 存在时：icon_url 指向 /api/brand/icon，source = "user"
    - 否则：icon_url 仍指向 /api/brand/icon（服务端内部回退），source = "default"

    这样前端不需要关心当前是用户上传还是默认，<img src> 始终用同一个 URL 即可。
    """
    if BRAND_ICON_PATH.exists() and BRAND_ICON_PATH.is_file():
        return {
            "icon_url": "/api/brand/icon",
            "icon_source": "user",
        }
    return {
        "icon_url": "/api/brand/icon",
        "icon_source": "default",
    }


# ============================================================
# API 端点
# ============================================================


@app.get("/api/brand", summary="获取品牌信息", tags=BRAND)
async def get_brand(request: Request):
    """获取品牌信息（公开接口，无需鉴权）。

    前端在加载界面（未登录）时就需要读取，与 theme_api.py 的 GET 保持一致。
    返回结构：
        {
            "status": 0,
            "msg": "ok",
            "data": {
                "title": "GsHub",
                "subtitle": "早柚核心",
                "icon_url": "/api/brand/icon",
                "icon_source": "user" | "default",
                "default": { ... DEFAULT_BRAND ... }
            }
        }
    """
    config = _read_brand_config()
    icon_info = _resolve_icon_info()
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "title": config["title"],
            "subtitle": config["subtitle"],
            "icon_url": icon_info["icon_url"],
            "icon_source": icon_info["icon_source"],
            # 附带默认值，方便前端做"恢复默认"按钮
            "default": dict(DEFAULT_BRAND),
        },
    }


@app.post("/api/brand", summary="更新品牌信息（标题 / 副标题）", tags=BRAND)
async def update_brand(
    request: Request,
    payload: BrandUpdateRequest,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """更新品牌 title / subtitle（需要鉴权）。

    ICON 不在此接口处理，请走 /api/brand/icon 上传/删除。
    """
    # 只持久化 title / subtitle；icon 由 /api/brand/icon 单独管理，读取时再合并默认值。
    data = {"title": payload.title, "subtitle": payload.subtitle}

    if not _write_brand_config(data):
        return {"status": 1, "msg": "保存失败"}

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "title": payload.title,
            "subtitle": payload.subtitle,
        },
    }


@app.post("/api/brand/icon", summary="上传品牌 ICON", tags=BRAND)
async def upload_brand_icon(
    request: Request,
    icon: UploadFile = File(..., description="PNG 格式，≤ 2MB"),
    _user: Dict[str, Any] = Depends(require_auth),
):
    """上传品牌 ICON（multipart，PNG，≤ 2MB，需要鉴权）。

    保存到 data/brand/ICON.png，下次 GET /api/brand 时立即生效。
    """
    # 1) 校验 MIME 类型：仅允许 image/png
    #    注意：浏览器可能不传或传错 content_type，因此 filename 后缀也作为兜底校验
    content_type = (icon.content_type or "").lower()
    filename = icon.filename or ""
    is_png = content_type == "image/png" or filename.lower().endswith(".png")
    if not is_png:
        return {
            "status": 1,
            "msg": f"不支持的图片格式: {content_type or 'unknown'}，仅允许 PNG",
        }

    # 2) 读取并校验大小
    try:
        content = await icon.read()
    except OSError as e:
        logger.warning(f"[Brand] 读取上传 ICON 失败: {e}")
        return {"status": 1, "msg": f"读取上传文件失败: {e}"}
    except Exception as e:
        # 保底：客户端断连等非预期异常也返回错误而非 500
        logger.exception(f"[Brand] 读取上传 ICON 未知错误: {e}")
        return {"status": 1, "msg": f"读取上传文件失败: {e}"}

    size = len(content)
    if size == 0:
        return {"status": 1, "msg": "上传的图片为空"}
    if size > _MAX_ICON_SIZE:
        size_mb = size / (1024 * 1024)
        return {
            "status": 1,
            "msg": f"图片过大 ({size_mb:.2f}MB)，上限 2MB",
        }

    # 3) 写入 data/brand/ICON.png（直接覆盖，下次 GET 立即生效）
    try:
        BRAND_ICON_PATH.parent.mkdir(parents=True, exist_ok=True)
        # atomic_save 接受路径 + 二进制流，避免写入过程中文件损坏
        with atomic_save(
            str(BRAND_ICON_PATH),
            text_mode=False,
            overwrite=True,
            file_perms=0o644,
        ) as file:
            if not file:
                raise RuntimeError("atomic_save 返回 None")
            file.write(content)
    except (OSError, RuntimeError) as e:
        logger.warning(f"[Brand] 写入 ICON 失败: {e}")
        return {"status": 1, "msg": f"保存 ICON 失败: {e}"}
    except Exception as e:
        # 保底：非预期异常也返回错误而非 500
        logger.exception(f"[Brand] 写入 ICON 未知错误: {e}")
        return {"status": 1, "msg": f"保存 ICON 失败: {e}"}

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "icon_url": "/api/brand/icon",
            "icon_source": "user",
            "size_bytes": size,
        },
    }


@app.delete("/api/brand/icon", summary="删除品牌 ICON", tags=BRAND)
async def delete_brand_icon(
    request: Request,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """删除用户上传的 ICON，回退到默认 CORE_PATH/ICON.png（需要鉴权）。"""
    if not BRAND_ICON_PATH.exists() or not BRAND_ICON_PATH.is_file():
        # 已是默认状态：直接返回成功，不报错
        return {
            "status": 0,
            "msg": "ok",
            "data": {"icon_source": "default"},
        }

    try:
        BRAND_ICON_PATH.unlink()
    except OSError as e:
        logger.warning(f"[Brand] 删除 ICON 失败: {e}")
        return {"status": 1, "msg": f"删除 ICON 失败: {e}"}
    except Exception as e:
        # 保底：非预期异常也返回错误而非 500
        logger.exception(f"[Brand] 删除 ICON 未知错误: {e}")
        return {"status": 1, "msg": f"删除 ICON 失败: {e}"}

    return {
        "status": 0,
        "msg": "ok",
        "data": {"icon_source": "default"},
    }


@app.get("/api/brand/icon", summary="获取品牌 ICON", tags=BRAND)
async def get_brand_icon(request: Request):
    """获取当前品牌 ICON（公开接口）。

    - 若 data/brand/ICON.png 存在：直接返回（用户上传）
    - 否则回退到 CORE_PATH/ICON.png（默认打包图标）
    - 都没有则返回 status=1
    """
    # URL 固定为 /api/brand/icon，换图后仍是同一地址；no-cache 强制浏览器按
    # ETag/Last-Modified 重校验，上传新图后能立即看到、未变则走 304 省流量。
    _no_cache = {"Cache-Control": "no-cache"}

    # 1) 用户上传的优先
    if BRAND_ICON_PATH.exists() and BRAND_ICON_PATH.is_file():
        return FileResponse(
            path=str(BRAND_ICON_PATH),
            media_type="image/png",
            filename="brand_ICON.png",
            headers=_no_cache,
        )

    # 2) 回退到默认 ICON（与 plugin_icon_api.py 同一文件源）
    default_icon: Path = CORE_PATH / "ICON.png"
    if default_icon.exists() and default_icon.is_file():
        return FileResponse(
            path=str(default_icon),
            media_type="image/png",
            filename="brand_ICON.png",
            headers=_no_cache,
        )

    # 3) 都没有：返回错误，前端可显示占位图标
    return {"status": 1, "msg": "品牌 ICON 不存在"}
