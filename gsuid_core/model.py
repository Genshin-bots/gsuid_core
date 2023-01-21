from typing import Optional, Any, List
from pydantic import BaseModel


class Message(BaseModel):
    type: Optional[str] = None
    data: Optional[Any] = None


class MessageReceive(BaseModel):
    bot: Optional[str] = None
    user_type: Optional[str] = None
    group_id: Optional[str] = None
    user_id: Optional[str] = None
    content: List[Message] = []


class MessageSend(BaseModel):
    bot: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    content: Optional[List[Message]] = None
