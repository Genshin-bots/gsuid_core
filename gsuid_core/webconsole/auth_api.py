"""
Auth APIs
提供认证相关的 RESTful APIs
"""

import secrets
from typing import Dict, Optional
from hashlib import sha256
from datetime import datetime, timedelta

import aiofiles
from fastapi import File, Header, Request, UploadFile
from sqlmodel import func, select

from gsuid_core.config import core_config
from gsuid_core.data_store import gs_data_path
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import verify_token, active_tokens, generate_token
from gsuid_core.utils.database.auth_models import WebUser
from gsuid_core.utils.database.base_models import async_maker

# Avatar storage path
AVATAR_PATH = gs_data_path / "avatars"


def get_register_code() -> str:
    """获取注册码"""
    return core_config.get_config("REGISTER_CODE")


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """哈希密码"""
    if salt is None:
        salt = secrets.token_hex(16)
    password_hash = sha256((password + salt).encode()).hexdigest()
    return f"{salt}${password_hash}"


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码"""
    try:
        salt, hash_value = stored_hash.split("$")
        return hash_password(password, salt) == f"{salt}${hash_value}"
    except (ValueError, AttributeError):
        return False


async def get_user_by_email(email: str) -> Optional[WebUser]:
    """从数据库获取用户"""
    return await WebUser.get_user_by_email(email=email)


async def create_user_in_db(
    email: str,
    name: str,
    password: str,
    role: str = "user",
) -> Optional[WebUser]:
    """在数据库中创建用户"""
    password_hash = hash_password(password)
    async with async_maker() as session:
        user = WebUser(
            email=email,
            name=name,
            password_hash=password_hash,
            role=role,
            avatar=None,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def update_user_avatar_in_db(email: str, avatar_url: str) -> int:
    """在数据库中更新用户头像"""
    return await WebUser.update_avatar(email=email, avatar_url=avatar_url)


async def get_admin_count() -> int:
    """获取数据库中admin用户的数量"""
    async with async_maker() as session:
        result = await session.execute(select(func.count()).select_from(WebUser).where(WebUser.role == "admin"))
        return result.scalar() or 0


@app.post("/api/auth/login")
async def api_login(request: Request, data: Dict):
    """Frontend login endpoint - generates token"""

    email = data.get("email", "")
    password = data.get("password", "")

    if not email or not password:
        return {"status": 1, "msg": "请输入邮箱和密码"}

    # 从数据库获取用户
    user = await get_user_by_email(email)

    if user and verify_password(password, user.password_hash):
        # Generate token
        token = generate_token(email)

        # Store token
        active_tokens[token] = {
            "user": {
                "id": str(user.id),
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "avatar": user.avatar,
            },
            "expires": datetime.now() + timedelta(hours=24),
        }

        return {
            "status": 0,
            "msg": "登录成功",
            "data": {
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "name": user.name,
                    "role": user.role,
                    "avatar": user.avatar,
                },
                "token": token,
            },
        }

    return {"status": 1, "msg": "邮箱或密码错误"}


@app.post("/api/auth/register")
async def api_register(request: Request, data: Dict):
    """Frontend registration endpoint - creates new user"""

    name = data.get("name", "")
    email = data.get("email", "")
    password = data.get("password", "")
    register_code = data.get("register_code", "")
    is_admin = data.get("is_admin", False)

    if not name or not email or not password or not register_code:
        return {"status": 1, "msg": "请填写所有必填项"}

    # 验证注册码
    expected_code = get_register_code()
    if register_code != expected_code:
        return {"status": 1, "msg": "注册码错误"}

    # 检查邮箱格式
    if "@" not in email or "." not in email.split("@")[-1]:
        return {"status": 1, "msg": "请输入有效的邮箱地址"}

    # 检查密码强度
    if len(password) < 6:
        return {"status": 1, "msg": "密码长度至少6位"}

    # 检查用户是否已存在
    existing_user = await get_user_by_email(email)
    if existing_user:
        return {"status": 1, "msg": "该邮箱已被注册"}

    # 检查管理员数量
    admin_count = await get_admin_count()

    # 如果已经有管理员，且当前请求尝试注册为管理员，则拒绝
    if admin_count >= 1 and is_admin:
        return {"status": 1, "msg": "管理员已存在，无法再次注册管理员账号"}

    # 设置角色：如果是系统首位用户，强制设为管理员；否则根据 is_admin 标志（此时 is_admin 必为 False 或未提供）
    user_role = "admin" if admin_count == 0 else "user"

    # 创建新用户
    user = await create_user_in_db(
        email=email,
        name=name,
        password=password,
        role=user_role,
    )

    if user:
        # Generate token
        token = generate_token(email)

        # Store token
        active_tokens[token] = {
            "user": {
                "id": str(user.id),
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "avatar": user.avatar,
            },
            "expires": datetime.now() + timedelta(hours=24),
        }

        return {
            "status": 0,
            "msg": "注册成功",
            "data": {
                "user": {
                    "id": str(user.id),
                    "email": user.email,
                    "name": user.name,
                    "role": user.role,
                    "avatar": user.avatar,
                },
                "token": token,
            },
        }

    return {"status": 1, "msg": "注册失败，请稍后重试"}


@app.get("/api/auth/admin/exists")
async def check_admin_exists(request: Request):
    """
    检查管理员是否已存在

    用于前端判断是否显示管理员注册入口

    Returns:
        status: 0 表示成功
        data:
            is_admin_exist: true 表示管理员已存在，false 表示管理员不存在
    """
    admin_count = await get_admin_count()
    is_admin_exist = admin_count >= 1

    return {"status": 0, "msg": "查询成功", "data": {"is_admin_exist": is_admin_exist}}


@app.post("/api/auth/logout")
async def api_logout(request: Request, authorization: str | None = Header(default=None)):
    """Frontend logout endpoint"""
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
        if token in active_tokens:
            del active_tokens[token]
    return {"status": 0, "msg": "已退出登录"}


@app.get("/api/auth/me")
async def get_current_user(request: Request, authorization: str | None = Header(default=None)):
    """Get current user info"""
    user_data = verify_token(authorization)
    if not user_data:
        return {"status": 1, "msg": "未授权", "data": None}

    # 从数据库获取最新用户信息
    email = user_data["user"]["email"]
    db_user = await get_user_by_email(email)
    if db_user:
        user_data["user"]["name"] = db_user.name
        user_data["user"]["role"] = db_user.role
        user_data["user"]["avatar"] = db_user.avatar

    return {
        "status": 0,
        "msg": "ok",
        "data": user_data["user"],
    }


@app.post("/api/auth/avatar")
async def upload_avatar(
    avatar: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Upload user avatar"""
    user_data = verify_token(authorization)
    if not user_data:
        return {"status": 1, "msg": "未授权", "data": None}

    # Create avatar directory if not exists
    AVATAR_PATH.mkdir(parents=True, exist_ok=True)

    # Read the multipart form data
    try:
        # Get user email
        user_email = user_data["user"]["email"]

        # Get filename and extension
        avatar_filename = avatar.filename or "avatar.png"
        ext = avatar_filename.split(".")[-1] if "." in avatar_filename else "png"

        # Generate filename
        filename = f"{user_email.replace('@', '_at_')}.{ext}"
        file_path = AVATAR_PATH / filename

        # Save file
        content = await avatar.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)

        # Update user avatar in database
        avatar_url = f"/api/auth/avatar/{filename}"
        await update_user_avatar_in_db(user_email, avatar_url)

        # Update in active_tokens
        for token, data in active_tokens.items():
            if data["user"]["email"] == user_email:
                data["user"]["avatar"] = avatar_url

        return {"status": 0, "msg": "头像上传成功", "data": {"avatar": avatar_url}}
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to upload avatar: {e}")
        return {"status": 1, "msg": "头像上传失败"}


@app.get("/api/auth/avatar/{filename}")
async def get_avatar(request: Request, filename: str):
    """Serve avatar files"""
    file_path = AVATAR_PATH / filename
    if not file_path.exists():
        return {"status": 1, "msg": "头像不存在"}

    try:
        async with aiofiles.open(file_path, "rb") as f:
            content = await f.read()

        # Determine content type
        ext = filename.split(".")[-1].lower() if "." in filename else "png"
        content_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/png")

        from fastapi.responses import Response

        return Response(content=content, media_type=content_type)
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to get avatar: {e}")
        return {"status": 1, "msg": "获取头像失败"}


@app.post("/api/auth/name")
async def update_name(request: Request, data: Dict, authorization: str | None = Header(default=None)):
    """Update user name"""
    user_data = verify_token(authorization)
    if not user_data:
        return {"status": 1, "msg": "未授权", "data": None}

    name = data.get("name", "")
    if not name or len(name.strip()) == 0:
        return {"status": 1, "msg": "用户名不能为空"}

    if len(name) > 50:
        return {"status": 1, "msg": "用户名不能超过50个字符"}

    user_email = user_data["user"]["email"]

    # Update name in database
    result = await WebUser.update_name(email=user_email, name=name.strip())
    if result == 0:
        # Update in active_tokens
        for token, tdata in active_tokens.items():
            if tdata["user"]["email"] == user_email:
                tdata["user"]["name"] = name.strip()

        return {"status": 0, "msg": "用户名更新成功", "data": {"name": name.strip()}}

    return {"status": 1, "msg": "用户名更新失败"}


@app.post("/api/auth/password")
async def update_password(request: Request, data: Dict, authorization: str | None = Header(default=None)):
    """Update user password"""
    user_data = verify_token(authorization)
    if not user_data:
        return {"status": 1, "msg": "未授权", "data": None}

    old_password = data.get("old_password", "")
    new_password = data.get("new_password", "")

    if not old_password or not new_password:
        return {"status": 1, "msg": "请输入旧密码和新密码"}

    if len(new_password) < 6:
        return {"status": 1, "msg": "新密码长度至少6位"}

    user_email = user_data["user"]["email"]

    # Verify old password
    user = await get_user_by_email(user_email)
    if not user or not verify_password(old_password, user.password_hash):
        return {"status": 1, "msg": "旧密码错误"}

    # Update password in database
    new_password_hash = hash_password(new_password)
    result = await WebUser.update_password(email=user_email, new_password_hash=new_password_hash)
    if result == 0:
        return {"status": 0, "msg": "密码更新成功"}

    return {"status": 1, "msg": "密码更新失败"}
