from __future__ import annotations

from typing import Dict, List, TypedDict


class HardChallenge(TypedDict):
    """主页 index 里的幽境危战摘要。"""

    difficulty: int
    name: str
    has_data: bool
    is_unlock: bool


class HardChallengeSchedule(TypedDict):
    start_time: str
    end_time: str


class HardChallengeBest(TypedDict):
    difficulty: int
    second: int


class HardChallengeMonster(TypedDict):
    name: str
    icon: str
    level: int


class HardChallengeTeamAvatar(TypedDict):
    avatar_id: int
    image: str
    level: int
    rarity: int
    rank: int


class HardChallengeFloor(TypedDict):
    name: str
    second: int
    monster: HardChallengeMonster
    best_avatar: List[Dict]
    teams: List[HardChallengeTeamAvatar]


class HardChallengeSingle(TypedDict):
    best: HardChallengeBest
    challenge: List[HardChallengeFloor]


class HardChallengeEntry(TypedDict):
    schedule: HardChallengeSchedule
    single: HardChallengeSingle


class HardChallengeData(TypedDict):
    """hard_challenge 接口 data 字段。"""

    data: List[HardChallengeEntry]


class HardChallengeDetail(TypedDict):
    is_unlock: bool
    difficulty: int
    second: int
    icon: str
    sub: SubHardDetail


class SubHardDetail(TypedDict):
    seconds: int
    x: int
    y: int
