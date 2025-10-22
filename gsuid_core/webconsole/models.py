from __future__ import annotations

from typing import Dict, List, Literal, TypedDict


class Task(TypedDict):
    label: str
    key: str
    status: int
    remark: str


class CheckBox(TypedDict):
    label: str
    value: str


class Option(TypedDict, total=False):
    label: str
    value: str  # 路径字符串
    children: List[Option]  # 递归引用 Option 自身


class TreeData(TypedDict):
    type: Literal["input-tree"]
    name: str
    label: str
    multiple: bool
    options: List[Option]
    heightAuto: bool
    virtualThreshold: int
    initiallyOpen: bool
    value: str
    searchable: bool
    wrapperCustomStyle: Dict
