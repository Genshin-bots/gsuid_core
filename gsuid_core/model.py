from typing import Any, List, Optional

from pydantic import BaseModel


class Message(BaseModel):
    type: Optional[str] = None
    data: Optional[Any] = None


class MessageReceive(BaseModel):
    bot: str = 'Bot'
    user_type: Optional[str] = None
    group_id: Optional[str] = None
    user_id: Optional[str] = None
    user_pm: int = 3
    content: List[Message] = []


class MessageContent(BaseModel):
    raw: Optional[MessageReceive] = None
    raw_text: str = ''
    command: Optional[str] = None
    text: Optional[str] = None
    image: Optional[str] = None
    at: Optional[str] = None
    image_list: List[Any] = []
    at_list: List[Any] = []


class MessageSend(BaseModel):
    bot: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None
