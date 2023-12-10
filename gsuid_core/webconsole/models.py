from typing import TypedDict


class Task(TypedDict):
    label: str
    key: str
    status: int
    remark: str
