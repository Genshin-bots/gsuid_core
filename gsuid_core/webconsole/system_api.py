"""
System APIs
提供系统信息相关的 RESTful APIs
"""

from typing import Dict

from fastapi import Depends, Request

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/system/info")
async def get_system_info(request: Request, _user: Dict = Depends(require_auth)):
    """Get system information"""
    from gsuid_core.version import __version__ as gscore_version

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "version": gscore_version,
            "python_version": "3.x",
            "uptime": "N/A",  # TODO: Add uptime tracking
        },
    }
