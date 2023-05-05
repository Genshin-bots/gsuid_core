from typing import TypedDict

from fastapi_user_auth.auth.models import User
from fastapi_amis_admin.models.fields import Field


class WebUser(User, table=True):
    bot_id: str = Field(None, title='用户平台')  # type:ignore
    user_id: str = Field(None, title='用户ID')  # type:ignore
    parent_id: int = Field(
        None, title='Superior', foreign_key='auth_user.id'
    )  # type:ignore


class Task(TypedDict):
    label: str
    key: str
    status: int
    remark: str
