"""
Assets APIs
提供图片上传、预览等资源相关的 RESTful APIs
"""

import base64
from typing import Dict, Optional
from pathlib import Path

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


class UploadRequest(BaseModel):
    image: str  # Base64 string
    filename: str
    upload_to: Optional[str] = None
    target_filename: Optional[str] = None


@app.post("/api/assets/upload")
async def upload_asset(data: UploadRequest, _user: Dict = Depends(require_auth)):
    """上传图片并返回本地绝对路径和预览URL"""
    try:
        # 解析 Base64
        if "," in data.image:
            header, encoded = data.image.split(",", 1)
        else:
            encoded = data.image

        image_data = base64.b64decode(encoded)

        # 确定保存路径
        if data.upload_to:
            save_dir = Path(data.upload_to)
        else:
            from gsuid_core.data_store import gs_data_path

            save_dir = gs_data_path / "GsCore" / "uploads"

        if not save_dir.exists():
            save_dir.mkdir(parents=True, exist_ok=True)

        # 确定文件名
        if data.target_filename:
            # 如果指定了目标文件名，则使用它（通常是配置中的 filename + suffix）
            filename = data.target_filename
        else:
            filename = data.filename

        file_path = save_dir / filename

        # 保存文件（直接覆盖）
        with open(file_path, "wb") as f:
            f.write(image_data)

        abs_path = str(file_path.absolute())

        # 对路径进行 base64 编码用于预览
        encoded_path = base64.b64encode(abs_path.encode("utf-8")).decode()

        return {
            "status": 0,
            "msg": "上传成功",
            "data": {"path": abs_path, "url": f"/api/assets/preview?path={encoded_path}"},
        }
    except Exception as e:
        return {"status": 1, "msg": f"上传失败: {str(e)}"}


@app.get("/api/assets/preview")
async def preview_asset(path: str, token: Optional[str] = None):
    """预览本地图片"""
    # 验证 token
    from gsuid_core.webconsole.web_api import verify_token

    if not verify_token(token=token):
        raise HTTPException(status_code=403, detail="Authentication failed")

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
                    # 如果还是失败，尝试直接作为路径处理（兼容非编码路径）
                    real_path_str = path

        if "%" in real_path_str:
            real_path_str = urllib.parse.unquote(real_path_str)

        # 兼容相对路径，如果是相对路径，尝试从 data 目录查找
        real_path = Path(real_path_str)
        if not real_path.is_absolute():
            from gsuid_core.data_store import gs_data_path

            # 尝试在 data 目录下查找
            test_path = gs_data_path / real_path_str
            if test_path.exists():
                real_path = test_path

        if not real_path.exists() or not real_path.is_file():
            raise HTTPException(status_code=404, detail="图片不存在")

        return FileResponse(real_path)
    except Exception as e:
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=400, detail=f"预览失败: {str(e)}")
