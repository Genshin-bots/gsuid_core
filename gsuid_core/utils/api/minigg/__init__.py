"""
MiniGG API 包装：
原神基础信息 v4/v5；
原神语音；
原神地图；
"""

from .models import (  # noqa: F401
    Food as Food,  # noqa: F401
    Costs as Costs,  # noqa: F401
    Enemy as Enemy,  # noqa: F401
    Domain as Domain,  # noqa: F401
    Weapon as Weapon,  # noqa: F401
    Artifact as Artifact,  # noqa: F401
    Material as Material,  # noqa: F401
    Character as Character,  # noqa: F401
    WeaponStats as WeaponStats,  # noqa: F401
    CharacterStats as CharacterStats,  # noqa: F401
    CharacterTalents as CharacterTalents,  # noqa: F401
    CharacterConstellations as CharacterConstellations,
)
from .request import (  # noqa: F401
    get_map_data as get_map_data,  # noqa: F401
    get_audio_info as get_audio_info,  # noqa: F401
    get_others_info as get_others_info,  # noqa: F401
    get_talent_info as get_talent_info,  # noqa: F401
    get_weapon_info as get_weapon_info,  # noqa: F401
    get_weapon_costs as get_weapon_costs,  # noqa: F401
    get_weapon_stats as get_weapon_stats,  # noqa: F401
    get_character_info as get_character_info,  # noqa: F401
    get_character_costs as get_character_costs,  # noqa: F401
    get_character_stats as get_character_stats,  # noqa: F401
    get_constellation_info as get_constellation_info,
)
from .exception import MiniggNotFoundError as MiniggNotFoundError  # noqa: F401

__all__ = ["request", "exception", "models"]
