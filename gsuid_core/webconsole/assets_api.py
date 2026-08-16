"""
Assets APIs
提供图片上传、预览等资源相关的 RESTful APIs
只允许操作 ``data/`` 目录内的文件。
"""

import base64
from typing import Any, Dict, Optional
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from gsuid_core.data_store import gs_data_path
from gsuid_core.utils.path_safety import PathEscapeError, safe_join, is_safe_filename, resolve_under_root
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import ASSETS


class UploadRequest(BaseModel):
    image: str  # Base64 string
    filename: str
    upload_to: Optional[str] = None
    target_filename: Optional[str] = None


def _resolve_asset_path(raw: str) -> Path:
    # 前端 CoreConfigPage 把 data 根写成相对名 "data"
    return resolve_under_root(raw, gs_data_path, aliases=frozenset({".", "data"}))


@app.post("/api/assets/upload", summary="上传图片", tags=ASSETS)
async def upload_asset(
    data: UploadRequest,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    上传图片并返回本地绝对路径和预览URL

    接收 Base64 编码的图片数据并保存到服务器，返回访问路径和预览 URL。
    保存位置必须落在 data/ 目录内。
    """
    try:
        # 解析 Base64
        if "," in data.image:
            header, encoded = data.image.split(",", 1)
        else:
            encoded = data.image

        image_data = base64.b64decode(encoded)

        # 确定保存路径
        if data.upload_to:
            save_dir = _resolve_asset_path(data.upload_to)
        else:
            save_dir = gs_data_path / "GsCore" / "uploads"
            save_dir.mkdir(parents=True, exist_ok=True)
            save_dir = save_dir.resolve()

        if not save_dir.exists():
            save_dir.mkdir(parents=True, exist_ok=True)

        # 确定文件名
        if data.target_filename:
            filename = Path(data.target_filename).name
        else:
            filename = Path(data.filename).name
        if not is_safe_filename(filename):
            return {"status": 1, "msg": "非法文件名"}

        file_path = safe_join(save_dir, filename)

        # 保存文件（直接覆盖）
        with open(file_path, "wb") as f:
            f.write(image_data)

        abs_path = str(file_path)

        # 对路径进行 base64 编码用于预览
        encoded_path = base64.b64encode(abs_path.encode("utf-8")).decode()

        return {
            "status": 0,
            "msg": "上传成功",
            "data": {"path": abs_path, "url": f"/api/assets/preview?path={encoded_path}"},
        }
    except PathEscapeError:
        return {"status": 1, "msg": "非法路径"}
    except Exception as e:
        return {"status": 1, "msg": f"上传失败: {str(e)}"}


@app.get("/api/assets/preview", summary="预览图片", tags=ASSETS)
async def preview_asset(path: str, _user: Dict[str, Any] = Depends(require_auth)):
    """
    预览本地图片（仅 data/ 目录内）
    """

    try:
        import urllib.parse

        # 解码路径
        try:
            # 尝试标准 Base64 解码
            real_path_str = base64.b64decode(path).decode("utf-8")
        except Exception:
            try:
                # 尝试 URL 安全的 Base64 解码
                real_path_str = base64.urlsafe_b64decode(path).decode("utf-8")
            except Exception:
                # 尝试处理可能存在的填充问题
                try:
                    padding = "=" * (4 - len(path) % 4)
                    if padding == "====":
                        padding = ""
                    real_path_str = base64.b64decode(path + padding).decode("utf-8")
                except Exception:
                    real_path_str = path

        if "%" in real_path_str:
            real_path_str = urllib.parse.unquote(real_path_str)

        real_path = _resolve_asset_path(real_path_str)

        if not real_path.exists() or not real_path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在")

        return FileResponse(real_path)
    except PathEscapeError:
        raise HTTPException(status_code=400, detail="非法路径")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"预览失败: {str(e)}")


@app.delete("/api/assets/delete", summary="删除图片", tags=ASSETS)
async def delete_asset(path: str, _user: Dict[str, Any] = Depends(require_auth)):
    """
    删除本地图片（仅 data/ 目录内）
    """
    try:
        import urllib.parse

        real_path_str = urllib.parse.unquote(path)
        real_path = _resolve_asset_path(real_path_str)

        if not real_path.exists() or not real_path.is_file():
            return {"status": 1, "msg": "文件不存在"}

        real_path.unlink()

        return {"status": 0, "msg": "删除成功"}
    except PathEscapeError:
        return {"status": 1, "msg": "非法路径"}
    except Exception as e:
        return {"status": 1, "msg": f"删除失败: {str(e)}"}
