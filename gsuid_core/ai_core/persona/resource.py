"""
角色资源管理模块

负责角色数据的持久化存储和加载，支持：
- 保存角色资料到本地文件系统
- 从本地文件系统加载角色资料
- 列出所有可用的角色名称
- 管理角色的头像、立绘、音频等资源文件

此模块提供向后兼容的函数接口，内部使用Persona类实现
"""

import re
import json
from typing import Dict, Tuple, Optional
from pathlib import Path

from gsuid_core.logger import logger

from .persona import Persona

# voice_anchor 缓存 {persona_name: str}，避免每轮对话重复读盘
_voice_anchor_cache: Dict[str, str] = {}

# 兜底正则：从 persona.md 抓取最具描述性的一行作为口吻锚点。
# 优先级：Style (风格) 块 > Tone Markers (语气词) 块 > Identity 行。
# 设计参考 ``persona/prompts.py`` 的 ``sayu_persona_prompt`` / CHARACTER_BUILDING_TEMPLATE。
#
# 两种书写格式都支持：
#   - 块形式：``Style (风格):\n        <content>\n        <content>``
#   - 行内形式：``Style (风格): <content>``（如爱弥斯人格）
# 块正则用 ``^(\s*)<header>:\n((?:\1[ \t]+...)+)`` 的形式：内容行必须比 header
# **更深**一级缩进，避免捕到下一个同级 section header（如 ``Style (风格):``
# 紧跟 ``Tone Markers (语气词):`` 时不串行）。
_STYLE_BLOCK_RE = re.compile(
    r"^([ \t]*)Style\s*\(\s*风格\s*\)\s*:[ \t]*\n((?:\1[ \t]+[^\n]+\n?)+)",
    re.IGNORECASE | re.MULTILINE,
)
_STYLE_INLINE_RE = re.compile(
    r"Style\s*\(\s*风格\s*\)\s*:[ \t]+([^\n]+)",
    re.IGNORECASE,
)
_TONE_BLOCK_RE = re.compile(
    r"^([ \t]*)Tone\s*Markers?\s*\(\s*语气词\s*\)\s*:[ \t]*\n((?:\1[ \t]+[^\n]+\n?)+)",
    re.IGNORECASE | re.MULTILINE,
)
_TONE_INLINE_RE = re.compile(
    r"Tone\s*Markers?\s*\(\s*语气词\s*\)\s*:[ \t]+([^\n]+)",
    re.IGNORECASE,
)
_IDENTITY_RE = re.compile(r"Identity\s*:[ \t]*([^\n]+)", re.IGNORECASE)

# compact persona 抽取：心跳决策只需要"我是谁 / 怎么说话 / 何时开口"四要素，
# 不需要工具协议、好感度梯度、触发例等执行细节。下列正则与上方块/行版本
# 配套使用，行匹配优先级 Name / Identity / Interest 顺序。
_NAME_RE = re.compile(r"Name\s*:[ \t]*([^\n]+)", re.IGNORECASE)
_INTEREST_RE = re.compile(r"Interest\s*:[ \t]*([^\n]+)", re.IGNORECASE)
# Presence 块下面常见两行：感兴趣的话题、主动发言示例。这两行最能帮 Heartbeat
# 模型理解"何时插话、说什么"，比整段 Presence 表更紧凑。
_PRESENCE_TOPIC_RE = re.compile(r"感兴趣的话题\s*[：:][ \t]*([^\n]+)")
_PRESENCE_EXAMPLE_RE = re.compile(r"主动发言示例\s*[：:][ \t]*([^\n]+)")

# 跳过模板占位符 / 通用说明 / 示例 / 元提示行（这些不是真正的"风格描述"）
_META_LINE_PREFIXES: Tuple[str, ...] = (
    "举出",
    "举例",
    "如：",
    "如:",
    "例：",
    "例:",
    "示例",
    "<example>",
    "禁止：",
    "禁止:",
    "允许：",
    "允许:",
    "要求：",
    "要求:",
    "注意：",
    "注意:",
)


def _pick_concrete_line(block: str) -> str:
    """从块文本里挑出最具描述性的一行——非空、非占位符、非元说明，
    取最长的一条（信息量最大）。
    """
    candidates: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "[SLOT:" in line:
            continue
        if any(line.startswith(p) for p in _META_LINE_PREFIXES):
            continue
        candidates.append(line)
    return max(candidates, key=len) if candidates else ""


def _extract_voice_anchor_from_persona(persona_text: str) -> str:
    """从人格文本中正则提取一行作为兜底口吻锚点。

    ``Style (风格):`` 块下的说话风格描述最贴口吻，是首选；都没有则回退
    ``Tone Markers (语气词):`` 块；再回退 ``Identity:`` 单行。
    """
    if not persona_text:
        return ""

    # Style: 块形式优先，回退行内形式
    style_match = _STYLE_BLOCK_RE.search(persona_text)
    if style_match:
        line = _pick_concrete_line(style_match.group(2))
        if line:
            return line
    style_inline = _STYLE_INLINE_RE.search(persona_text)
    if style_inline:
        line = style_inline.group(1).strip()
        if line and "[SLOT:" not in line:
            return line

    # Tone Markers: 块形式优先，回退行内形式
    tone_match = _TONE_BLOCK_RE.search(persona_text)
    if tone_match:
        line = _pick_concrete_line(tone_match.group(2))
        if line:
            return f"语气词常用：{line}"
    tone_inline = _TONE_INLINE_RE.search(persona_text)
    if tone_inline:
        line = tone_inline.group(1).strip()
        if line and "[SLOT:" not in line:
            return f"语气词常用：{line}"

    identity_match = _IDENTITY_RE.search(persona_text)
    if identity_match:
        candidate = identity_match.group(1).strip()
        if candidate and "[SLOT:" not in candidate:
            return candidate

    return ""


def extract_compact_persona(persona_text: str) -> str:
    """从完整 persona.md 中提取心跳决策所需的压缩版人格描述。

    决策阶段只判断"该不该说话、用什么口吻"，无需工具协议 / 好感度梯度 /
    触发例等执行细节，把整篇 persona.md 灌进去会产生大量重复 token。
    本函数按以下顺序提取四要素，全部失败时返回空串由调用方回退到原文：

        [身份] Name / Identity / Interest
        [风格] Style (风格) 块的最具描述性一行
        [语气] Tone Markers (语气词) 块的最具描述性一行
        [活跃] 感兴趣的话题 + 主动发言示例 两行

    Args:
        persona_text: 完整 persona.md 文本

    Returns:
        压缩后的纯文本片段（约 200-600 字符），无任何匹配时返回空串。
    """
    if not persona_text:
        return ""

    sections: list[str] = []

    # 1. [身份] —— Name / Identity / Interest 三行合并
    identity_parts: list[str] = []
    name_match = _NAME_RE.search(persona_text)
    if name_match:
        v = name_match.group(1).strip()
        if v and "[SLOT:" not in v:
            identity_parts.append(f"Name: {v}")
    identity_match = _IDENTITY_RE.search(persona_text)
    if identity_match:
        v = identity_match.group(1).strip()
        if v and "[SLOT:" not in v:
            identity_parts.append(f"Identity: {v}")
    interest_match = _INTEREST_RE.search(persona_text)
    if interest_match:
        v = interest_match.group(1).strip()
        if v and "[SLOT:" not in v:
            identity_parts.append(f"Interest: {v}")
    if identity_parts:
        sections.append("[身份] " + " / ".join(identity_parts))

    # 2. [风格] —— Style 块（块形式优先，回退行内）
    style_line = ""
    style_match = _STYLE_BLOCK_RE.search(persona_text)
    if style_match:
        style_line = _pick_concrete_line(style_match.group(2))
    if not style_line:
        style_inline = _STYLE_INLINE_RE.search(persona_text)
        if style_inline:
            cand = style_inline.group(1).strip()
            if cand and "[SLOT:" not in cand:
                style_line = cand
    if style_line:
        sections.append(f"[风格] {style_line}")

    # 3. [语气] —— Tone Markers 块（块形式优先，回退行内）
    tone_line = ""
    tone_match = _TONE_BLOCK_RE.search(persona_text)
    if tone_match:
        tone_line = _pick_concrete_line(tone_match.group(2))
    if not tone_line:
        tone_inline = _TONE_INLINE_RE.search(persona_text)
        if tone_inline:
            cand = tone_inline.group(1).strip()
            if cand and "[SLOT:" not in cand:
                tone_line = cand
    if tone_line:
        sections.append(f"[语气] {tone_line}")

    # 4. [活跃] —— Presence 中的"感兴趣的话题" + "主动发言示例"两行
    presence_parts: list[str] = []
    topic_match = _PRESENCE_TOPIC_RE.search(persona_text)
    if topic_match:
        v = topic_match.group(1).strip()
        if v and "[SLOT:" not in v:
            presence_parts.append(f"感兴趣的话题：{v}")
    example_match = _PRESENCE_EXAMPLE_RE.search(persona_text)
    if example_match:
        v = example_match.group(1).strip()
        if v and "[SLOT:" not in v:
            presence_parts.append(f"主动发言示例：{v}")
    if presence_parts:
        sections.append("[活跃] " + " / ".join(presence_parts))

    return "\n".join(sections)


# voice_anchor 显式手调入口的文件名。
# 不再放进 ``config.json`` —— 后者由 ``StringConfig`` 用严格的 ``Dict[str, GSC]``
# schema 加载, 任何非结构化字段会直接触发 ``update_config -> repair_config``
# 死循环 (历史上早柚的 voice_anchor 裸字符串就栽过这个坑)。
_VOICE_ANCHOR_FILENAME = "voice_anchor.txt"


def migrate_voice_anchor_from_config(persona_name: str) -> bool:
    """一次性迁移：把旧版本写在 ``config.json`` 的 ``voice_anchor`` 字段搬到
    独立的 ``voice_anchor.txt``, 并从 JSON 里抹掉该键。

    必须在任何 ``StringConfig`` (经 ``PersonaConfigManager``) 加载 persona
    ``config.json`` 之前完成, 否则严校验会拒收裸字符串、触发死循环。

    幂等性：
    - ``voice_anchor.txt`` 已存在 → 新位置优先, 不动旧字段（仍会清理 JSON 里
      残留的同名键, 避免日后再次触发 StringConfig 校验异常）。
    - ``config.json`` 不存在 / 无该字段 → 直接返回 False。

    Returns:
        是否实际写出了 ``voice_anchor.txt`` 新文件。
    """
    from ..resource import PERSONA_PATH

    persona_dir = PERSONA_PATH / persona_name
    txt_path = persona_dir / _VOICE_ANCHOR_FILENAME
    cfg_path = persona_dir / "config.json"

    if not cfg_path.exists():
        return False

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"🧠 [Persona] 读取 {cfg_path} 失败, 跳过 voice_anchor 迁移: {e}")
        return False

    if not isinstance(cfg, dict) or "voice_anchor" not in cfg:
        return False

    # 已确认 "voice_anchor" in cfg，直接访问（LLM.md §1.4：存在性检查后直接访问）
    raw = cfg["voice_anchor"]
    wrote_txt = False

    # 仅在 txt 尚未存在时, 把旧字段值写出（新位置以为先, 不覆盖用户手调）。
    if isinstance(raw, str) and raw.strip() and not txt_path.exists():
        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(raw.strip())
            wrote_txt = True
        except OSError as e:
            logger.warning(f"🧠 [Persona] 写出 {txt_path} 失败, 保留旧字段: {e}")
            # 写 txt 失败就不要继续删 JSON 字段, 留着等下次迁移
            return False

    # 不管 txt 是否本次新写, 都要把 JSON 里的旁路字段清掉, 避免 StringConfig 再炸。
    cfg.pop("voice_anchor", None)
    _write_json_atomic(cfg_path, cfg)

    if wrote_txt:
        logger.info(
            f"🧠 [Persona] 已将 '{persona_name}' 的 voice_anchor 从 config.json 迁出到 {_VOICE_ANCHOR_FILENAME}"
        )
    return wrote_txt


def _write_json_atomic(path: Path, data: dict) -> None:
    """通过 ``<path>.migrate`` 临时文件 + ``os.replace`` 原子写回 JSON, 失败不留半文件。"""
    import os

    tmp_path = path.with_suffix(path.suffix + ".migrate")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning(f"🧠 [Persona] 写回 {path} 失败: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _load_voice_anchor_from_disk(persona_name: str) -> str:
    """读盘装配 voice_anchor。三级优先级:

    1. ``voice_anchor.txt`` —— 用户显式手调入口 (与 persona 配置物理解耦)。
    2. ``persona.md`` 正则提取 ``Style (风格)`` / ``Tone Markers (语气词)`` /
       ``Identity:`` 一行作为兜底。
    3. 都无 → 返回空串 (``handle_ai.py`` 自动跳过锚点注入)。

    ``config.json`` **不再**参与 voice_anchor 解析 —— 那个文件归 ``StringConfig``
    严格 schema 管控, 旁路字段会触发加载死循环。历史数据由 ``migrate_voice_anchor_from_config``
    在启动期一次性搬走。
    """
    from ..resource import PERSONA_PATH

    persona_dir = PERSONA_PATH / persona_name

    # 1. 显式手调入口
    txt_path = persona_dir / _VOICE_ANCHOR_FILENAME
    if txt_path.exists():
        try:
            with open(txt_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                return raw
        except OSError as e:
            logger.debug(f"🧠 [Persona] 读取 {txt_path} 失败: {e}")

    # 2. persona.md 正则兜底
    md_path = persona_dir / "persona.md"
    if not md_path.exists():
        return ""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
    except OSError as e:
        logger.debug(f"🧠 [Persona] 读取 {md_path} 失败: {e}")
        return ""
    return _extract_voice_anchor_from_persona(md_text)


def get_voice_anchor(persona_name: Optional[str]) -> str:
    """读取人格的逐轮口吻锚点（一句话口吻自述）。

    人格只在会话创建时把口吻固化进 system_prompt，会话越长越靠后、注意力
    越稀释，导致长会话人格漂移。``voice_anchor`` 由 ``handle_ai.py`` 每轮
    注入动态用户提示词上下文 (``rag_context``)，作为口吻锚点对冲漂移 ——
    **不修改** system_prompt / persona 定义本身。

    解析优先级：
    1. ``voice_anchor.txt`` 显式手调入口（最高优先级；与 persona 可变配置
       物理解耦, 不会被 ``StringConfig`` 当成结构化项校验）。
    2. 字段缺失时，从同目录 ``persona.md`` 用正则提取最有用的一行
       （``Style (风格):`` > ``Tone Markers (语气词):`` > ``Identity:``）。
    3. 两者都拿不到 → 返回空串（``handle_ai.py`` 会跳过锚点注入）。

    结果按 persona_name 缓存，避免每轮对话重复读盘 + 重复跑正则。
    """
    if not persona_name:
        return ""
    if persona_name in _voice_anchor_cache:
        return _voice_anchor_cache[persona_name]

    anchor = _load_voice_anchor_from_disk(persona_name)
    _voice_anchor_cache[persona_name] = anchor
    return anchor


def invalidate_voice_anchor_cache(persona_name: Optional[str] = None) -> None:
    """清理 voice_anchor 缓存：``persona_name`` 为 None 清全部，否则只清一项。
    供人格编辑 / 删除流程在持久化变更后调用，避免读到旧缓存。
    """
    if persona_name is None:
        _voice_anchor_cache.clear()
        return
    _voice_anchor_cache.pop(persona_name, None)


async def save_persona(char_name: str, profile_content: str) -> None:
    """
    保存角色资料到本地存储

    将角色资料以Markdown格式持久化存储到本地文件系统。
    每个角色在data/ai_core/persona下有自己的独立文件夹。

    Args:
        char_name: 角色名称，用于作为文件夹名
        profile_content: 角色资料内容（Markdown格式）
    """
    persona = Persona(char_name)
    await persona.save_content(profile_content)
    # persona.md 变更后，可能改变正则兜底出的口吻锚点，清缓存让下次重读
    invalidate_voice_anchor_cache(char_name)


async def load_persona(char_name: str) -> str:
    """
    从本地存储加载角色资料

    根据角色名称读取对应的角色资料文件。

    Args:
        char_name: 角色名称

    Returns:
        角色资料内容（Markdown格式字符串）

    Raises:
        FileNotFoundError: 如果角色资料文件不存在
    """
    if char_name == "智能助手":
        return "你是一个智能助手，简短的一句话回答问题即可。"

    persona = Persona(char_name)
    return await persona.load_content()


def list_available_personas() -> list[str]:
    """
    列出所有可用的角色名称

    扫描角色存储目录，返回所有已存储的角色名称列表。

    Returns:
        角色名称列表（不含文件扩展名）
    """
    return Persona.list_all_names()


def get_persona_avatar_path(char_name: str) -> Optional[str]:
    """
    获取角色的头像图片路径

    查找角色文件夹下的 avatar.png 文件。

    Args:
        char_name: 角色名称

    Returns:
        头像图片的绝对路径字符串，如果不存在则返回 None
    """
    persona = Persona(char_name)
    return persona.get_avatar_path()


def get_persona_image_path(char_name: str) -> Optional[str]:
    """
    获取角色的立绘图片路径

    查找角色文件夹下的 image.png 文件。

    Args:
        char_name: 角色名称

    Returns:
        立绘图片的绝对路径字符串，如果不存在则返回 None
    """
    persona = Persona(char_name)
    return persona.get_image_path()


def get_persona_audio_path(char_name: str) -> Optional[str]:
    """
    获取角色的音频文件路径

    查找角色文件夹下的 audio.mp3 文件。

    Args:
        char_name: 角色名称

    Returns:
        音频文件的绝对路径字符串，如果不存在则返回 None
    """
    persona = Persona(char_name)
    return persona.get_audio_path()


def delete_persona(char_name: str) -> bool:
    """
    删除角色资料和相关文件

    删除 persona 目录下该角色的整个文件夹，包括配置文件。

    Args:
        char_name: 角色名称

    Returns:
        True 如果成功删除，False 如果角色不存在
    """
    # 先删除配置文件
    from .config import persona_config_manager

    persona_config_manager.delete_persona_config(char_name)

    persona = Persona(char_name)
    deleted = persona.delete()
    if deleted:
        invalidate_voice_anchor_cache(char_name)
    return deleted


def get_persona_metadata(char_name: str) -> dict:
    """
    获取角色的元数据

    Args:
        char_name: 角色名称

    Returns:
        包含角色元数据的字典，包括：
        - name: 角色名称
        - has_avatar: 是否有头像
        - has_image: 是否有立绘
        - has_audio: 是否有音频
    """
    persona = Persona(char_name)
    return persona.get_metadata().to_dict()
