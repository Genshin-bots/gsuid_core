"""表情包模块配置项

使用 StringConfig 管理，配置文件名 meme_config，
存于 data/plugins_configs/meme_config.json。

部分参数为固定常量，不允许用户修改。
"""

from typing import Dict, List

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsIntConfig,
    GsBoolConfig,
    GsFloatConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

# ── 固定常量（不允许用户配置） ──

# 允许的图片 MIME 类型
MEME_ALLOWED_MIME: List[str] = ["image/jpeg", "image/png", "image/gif", "image/webp"]

# 每群每日自动采集上限
MEME_DAILY_COLLECT_LIMIT: int = 30

# 两次 VLM 打标之间的最小间隔（秒）
MEME_TAG_INTERVAL_SEC: int = 3

# 发送时排除最近 N 张已发送的图片
MEME_RECENT_EXCLUDE_COUNT: int = 10

# 每次检索的候选池大小（候选池内做加权随机采样，越大随机性/多样性越强）
MEME_PICK_CANDIDATE_COUNT: int = 12

# 单文件最大大小（KB），超过此大小的图片将被忽略
MEME_MAX_FILE_KB: int = 512

# 最小图片宽度（px）
MEME_MIN_WIDTH: int = 60

# 最小图片高度（px）
MEME_MIN_HEIGHT: int = 60

# ── 可配置项 ──

CONFIG_DEFAULT: Dict[str, GSC] = {
    "meme_enable": GsBoolConfig(
        title="启用表情包模块",
        desc="总开关，关闭后停止所有表情包采集和发送功能",
        data=True,
    ),
    "meme_vlm_enable": GsBoolConfig(
        title="启用 VLM 打标",
        desc="总开关，关闭后停止所有 VLM 打标功能",
        data=False,
    ),
    "meme_auto_collect": GsBoolConfig(
        title="自动采集群聊图片",
        desc="开启后自动监听群聊中的图片并入库",
        data=False,
    ),
    "meme_vlm_semaphore": GsIntConfig(
        title="VLM 打标并发上限",
        desc="同时进行 VLM 打标的任务数上限",
        data=1,
    ),
    "meme_nsfw_threshold": GsFloatConfig(
        title="NSFW 分数阈值(0~1)",
        desc="VLM 返回的 NSFW 分数超过此值时标记为 rejected",
        data=0.6,
    ),
    "meme_send_cooldown_sec": GsIntConfig(
        title="同一会话发图冷却(秒)",
        desc="同一会话两次发送表情包之间的最小间隔",
        data=60,
    ),
    "meme_search_threshold": GsFloatConfig(
        title="表情包搜索相似度阈值(0~1)",
        desc="向量检索的最低相似度分数，低于此值的结果将被视为不匹配。越高越严格，建议 0.3~0.6",
        data=0.4,
    ),
}

MEME_CONFIG_PATH = get_res_path("ai_core") / "meme_config.json"
meme_config = StringConfig(
    "GsCore AI 表情包配置",
    MEME_CONFIG_PATH,
    CONFIG_DEFAULT,
)
