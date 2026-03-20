"""
Auth Models
用于WebConsole认证的数据库模型
"""

from typing import Any, Dict, Optional
from datetime import datetime

from sqlmodel import Field, Index
from sqlalchemy.ext.asyncio import AsyncSession

from .base_models import BaseIDModel, with_session


class WebUser(BaseIDModel, table=True):
    """
    Web控制台用户表
    """

    __table_args__ = (
        Index("ix_webuser_email", "email", unique=True),
        {"extend_existing": True},
    )

    email: str = Field(title="邮箱", default=None, unique=True, index=True)
    name: str = Field(title="用户名", default=None)
    password_hash: str = Field(title="密码哈希", default=None)
    role: str = Field(title="角色", default="user")  # admin, user
    avatar: Optional[str] = Field(title="头像URL", default=None)
    created_at: datetime = Field(title="创建时间", default_factory=datetime.now)
    updated_at: datetime = Field(title="更新时间", default_factory=datetime.now)

    @classmethod
    @with_session
    async def create_user(
        cls,
        session: AsyncSession,
        email: str,
        name: str,
        password_hash: str,
        role: str = "user",
    ) -> "WebUser":
        """创建新用户"""
        user = cls(
            email=email,
            name=name,
            password_hash=password_hash,
            role=role,
            avatar=None,
        )
        session.add(user)
        return user

    @classmethod
    @with_session
    async def get_user_by_email(
        cls,
        session: AsyncSession,
        email: str,
    ) -> Optional["WebUser"]:
        """根据邮箱获取用户"""
        results = await cls.select_rows(email=email)
        if results:
            return results[0]
        return None

    @classmethod
    @with_session
    async def update_name(
        cls,
        session: AsyncSession,
        email: str,
        name: str,
    ) -> int:
        """更新用户名"""
        return await cls.update_data_by_data(
            select_data={"email": email},
            update_data={
                "name": name,
                "updated_at": datetime.now(),
            },
        )

    @classmethod
    @with_session
    async def update_password(
        cls,
        session: AsyncSession,
        email: str,
        new_password_hash: str,
    ) -> int:
        """更新用户密码"""
        return await cls.update_data_by_data(
            select_data={"email": email},
            update_data={
                "password_hash": new_password_hash,
                "updated_at": datetime.now(),
            },
        )

    @classmethod
    @with_session
    async def update_avatar(
        cls,
        session: AsyncSession,
        email: str,
        avatar_url: str,
    ) -> int:
        """更新用户头像"""
        return await cls.update_data_by_data(
            select_data={"email": email},
            update_data={
                "avatar": avatar_url,
                "updated_at": datetime.now(),
            },
        )

    @classmethod
    @with_session
    async def update_user_info(
        cls,
        session: AsyncSession,
        email: str,
        name: Optional[str] = None,
    ) -> int:
        """更新用户信息"""
        update_data: Dict[str, Any] = {"updated_at": datetime.now()}
        if name:
            update_data["name"] = name
        return await cls.update_data_by_data(
            select_data={"email": email},
            update_data=update_data,
        )
