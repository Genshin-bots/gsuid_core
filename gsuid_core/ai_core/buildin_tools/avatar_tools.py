"""
用户头像工具模块

让 Agent 按用户 ID 取某个用户的头像。取回后把头像注册进 RM 资源池并返回
图片ID（``img_xxxxxxxx``）——和群聊里的图片走同一套「只给 ID」的存储策略，
拿到 ID 后再按需消费：

- 想「看清」头像内容 → 交给 ``read_image('img_xxx')`` 转述。
- 想把头像发给用户 → 交给 ``send_message_by_ai(image_id='img_xxx')``。

头像来源复用框架既有的 ``get_event_avatar`` / ``get_qq_avatar`` /
``get_qqgroup_avatar``：

- 不传 user_id（或就是当前发言者）→ 走 ``get_event_avatar``，覆盖 sender.avatar
  直链与各平台兜底，跨平台通用。
- 指定他人 user_id → QQ 系平台（``onebot`` / ``qqgroup``）可按任意 ID 取头像；
  纯数字 ID 兜底按 QQ qlogo 取；其它平台无法按 ID 取他人头像时返回不支持说明。
"""

from typing import TYPE_CHECKING, Optional

import httpx
from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.image.convert import convert_img
from gsuid_core.utils.resource_manager import RM
from gsuid_core.utils.image.image_tools import (
    get_qq_avatar,
    get_event_avatar,
    get_qqgroup_avatar,
)

if TYPE_CHECKING:
    from PIL import Image


async def _resolve_avatar_image(ev: Event, target_user_id: Optional[str]) -> "Optional[Image.Image]":
    """解析目标用户头像为 PIL Image；取不到返回 None。

    - 目标是当前发言者（或未指定）：走 ``get_event_avatar``（自带各平台兜底，
      永远返回一张图，最差是默认占位图）。
    - 目标是他人：按 bot 平台取——onebot/qqgroup 走对应 qlogo 接口；其它平台
      仅在 ID 为纯数字时按 QQ qlogo 兜底尝试，否则返回 None。
    """
    is_self = (not target_user_id) or (str(target_user_id) == str(ev.user_id))
    if is_self:
        return await get_event_avatar(ev)

    target = str(target_user_id)
    if ev.bot_id == "onebot":
        return await get_qq_avatar(target)
    if ev.bot_id == "qqgroup":
        img = await get_qqgroup_avatar(ev.bot_self_id, target)
        if img is not None:
            return img

    # 兜底：纯数字 ID 仍按 QQ qlogo 尝试（覆盖部分 QQ 系 adapter）
    if target.isdigit():
        # 仅吞网络失败(httpx.HTTPError)与图片解析失败(qlogo 取不到时 404 体被当图片
        # 解析 → PIL.UnidentifiedImageError ⊂ OSError)；其余异常照常上抛，不做兜底。
        try:
            return await get_qq_avatar(target)
        except (httpx.HTTPError, OSError) as e:
            logger.debug(t("🧠 [BuildinTools] get_user_avatar qlogo 兜底失败: {e}", e=e))
            return None
    return None


@ai_tools(category="common")
async def get_user_avatar(
    ctx: RunContext[ToolContext],
    user_id: Optional[str] = None,
) -> str:
    """
    获取用户的头像

    取回指定用户（默认当前发言者）的头像，注册到资源池并返回图片ID。拿到 ID 后：
    想看清头像里是什么 → 调用 ``read_image(返回的ID)``；想把头像发出去 →
    用 ``send_message_by_ai(image_id=返回的ID)``。

    适用场景：用户问"看看我/他的头像""我的头像是什么""帮我把某人的头像发出来"，
    或你需要根据某人头像内容做出回应时。

    Args:
        ctx: 工具执行上下文
        user_id: 可选，目标用户ID。不传则取当前发言者；指定他人时，QQ 系平台
            （onebot / qqgroup）可按任意 ID 取头像。

    Returns:
        成功：包含资源ID（``img_xxxxxxxx``）及后续用法提示的说明文本；
        失败：中文错误/不支持说明。

    Example:
        >>> await get_user_avatar(ctx)  # 当前发言者头像
        >>> await get_user_avatar(ctx, user_id="123")  # 指定用户头像
    """
    ev = ctx.deps.ev
    if ev is None:
        return "❌ 无事件上下文，无法获取头像。"

    # 统一清洗：空 / 纯空白 user_id 归一为 None（视作取当前发言者），解析与
    # 日志/文案用同一个值，避免带空格的 ID 在解析与展示之间不一致。
    clean_user_id = (str(user_id).strip() or None) if user_id else None
    target = clean_user_id or str(ev.user_id)

    try:
        img = await _resolve_avatar_image(ev, clean_user_id)
    except (httpx.HTTPError, OSError) as e:
        logger.exception(t("🧠 [BuildinTools] get_user_avatar 获取用户 {target} 头像失败: {e}", target=target, e=e))
        return f"❌ 获取头像失败: {e}"

    if img is None:
        return f"⚠️ 当前平台（{ev.bot_id}）无法按 ID 获取用户 {target} 的头像，仅 QQ 系平台支持取任意用户头像。"

    try:
        data: bytes = await convert_img(img)  # PIL.Image → bytes（JPEG 编码）
    except OSError as e:
        logger.exception(t("🧠 [BuildinTools] get_user_avatar 头像编码失败: {e}", e=e))
        return f"❌ 头像编码失败: {e}"

    resource_id = RM.register(data)
    logger.info(
        t(
            "🧠 [BuildinTools] get_user_avatar: 用户 {target} 头像已注册到 RM: {resource_id}",
            target=target,
            resource_id=resource_id,
        )
    )
    return (
        f"已获取用户 {target} 的头像，资源ID: {resource_id}。\n"
        f"· 想看清头像内容 → read_image('{resource_id}')\n"
        f"· 想把头像发给用户 → send_message_by_ai(image_id='{resource_id}')"
    )
