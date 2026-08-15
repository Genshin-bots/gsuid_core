"""套件 / 插件 hint 的前缀标记。

作用不是「剥离」——``_relean_user_turn`` 整体替换第一个 ``UserPromptPart`` 为
``lean_content``，hint 天然不入 B 轨。前缀存在的意义是让 ``prepare_user`` 的
「框架注入识别」能把它们与用户原话区分开。
"""

PLUGIN_HINT_PREFIX = "（插件·"
KIT_HINT_PREFIX = "（套件·"

HINT_PREFIXES = (PLUGIN_HINT_PREFIX, KIT_HINT_PREFIX)


def is_hook_hint(text: str) -> bool:
    """判断一段文本是否为 hook 注入的 hint（非用户原话）。"""
    return text.lstrip().startswith(HINT_PREFIXES)


def format_hint(text: str, *, kit_id: str | None = None) -> str:
    """给 hint 套上来源前缀；已带前缀的原样返回（幂等，防重复注入时叠前缀）。"""
    body = text.strip()
    if not body or is_hook_hint(body):
        return body
    if kit_id and kit_id.startswith("gscore."):
        return f"{KIT_HINT_PREFIX}{kit_id.split('.', 1)[1]}：{body}）"
    owner = kit_id or "plugin"
    return f"{PLUGIN_HINT_PREFIX}{owner}：{body}）"
