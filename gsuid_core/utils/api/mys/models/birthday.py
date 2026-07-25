from __future__ import annotations

from typing import List, TypedDict


class GsRoleBirthDay(TypedDict):
    role_id: int
    name: str
    jump_tpye: str
    jump_target: str
    jump_start_time: str
    jump_end_time: str
    role_gender: int
    take_picture: str
    gal_xml: str
    gal_resource: str
    is_partake: bool
    bgm: str


class BsIndex(TypedDict):
    nick_name: str
    uid: int
    region: str
    role: List[GsRoleBirthDay]
    draw_notice: bool
    CurrentTime: str
    gender: int
    is_show_remind: bool


class RoleCalendar(TypedDict):
    role_id: int
    name: str
    role_birthday: str
    head_icon: str
    is_subscribe: bool


class RoleCalendarList(TypedDict):
    calendar_role: List[RoleCalendar]


MonthlyRoleCalendar = TypedDict(
    "MonthlyRoleCalendar",
    {
        "1": RoleCalendarList,
        "2": RoleCalendarList,
        "3": RoleCalendarList,
        "4": RoleCalendarList,
        "5": RoleCalendarList,
        "6": RoleCalendarList,
        "7": RoleCalendarList,
        "8": RoleCalendarList,
        "9": RoleCalendarList,
        "10": RoleCalendarList,
        "11": RoleCalendarList,
        "12": RoleCalendarList,
    },
)


class RolesCalendar(TypedDict):
    calendar_role_infos: MonthlyRoleCalendar
    is_pre: bool
    is_next: bool
    is_year_subscribe: bool
