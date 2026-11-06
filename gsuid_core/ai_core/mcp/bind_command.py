"""MCP 凭据绑定命令族（静态 SV，私聊限定）。

- ``mcp绑定`` 无参：列出可绑服务清单 + 绑定状态
- ``mcp绑定 <服务名>``：BIND_GUIDE（所需凭据项 + auth_help + 格式示例）
- ``mcp绑定 <服务名> <token|k=v ...>``：验活 → 加密落库 → 掩码回执
- ``mcp解绑 <服务名>``：删行 + 清 @user: 缓存
- ``mcp绑定列表``：已绑服务 + 掩码尾号 + 时间

全族仅私聊受理；群里发 → 提示私聊（群聊中 token 曝光风险另行提示）。
解析两遍：先取最长匹配的服务名前缀，剩余段再按 ``k=v`` 归凭据、
裸段归单 key token（服务名可带空格、token 可带 ``=``）。
"""

import time
from typing import Optional

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .client import MCPClient
from .config_manager import MCPConfig, mcp_config_manager
from .credential_store import (
    MCPUserCredential,
    masked_summary,
    sanitize_creds,
    mask_token_tail,
    render_client_kwargs,
)

sv_mcp_bind = SV("MCP凭据绑定", pm=6)


def _resolve_config(text: str) -> tuple[Optional[MCPConfig], Optional[str], str]:
    """按最长前缀匹配服务名（name / bind_alias / config_id）。

    返回 ``(config, config_id, 剩余参数字符串)``；无匹配 → ``(None, None, text)``。
    """
    query = text.strip()
    if not query:
        return None, None, ""
    best: tuple[Optional[MCPConfig], Optional[str], str] = (None, None, "")
    best_len = -1
    for cid, cfg in mcp_config_manager.list_config_items():
        names = [cfg.name, cid, *cfg.bind_alias]
        for name in names:
            if not name:
                continue
            if query == name or query.startswith(name + " ") or query.startswith(name + "　"):
                if len(name) > best_len:
                    best_len = len(name)
                    best = (cfg, cid, query[len(name) :].strip())
    return best


def _bindable_keys(config: MCPConfig) -> list[str]:
    """该服务需要用户绑定的凭据项：user_env_keys ∪ credential_params。"""
    keys = list(config.user_env_keys)
    for tool in config.tools:
        for p in tool.credential_params:
            if p not in keys:
                keys.append(p)
    return keys


def _parse_bind_args(rest: str, keys: list[str]) -> dict[str, str] | None:
    """解析凭据段：``k=v`` 归对应 key；裸段 → 单 key 服务的 token。

    必须一次绑齐：少 key → None。多出的未知段也拒绝（防误粘）。
    """
    if not keys:
        return {}
    creds: dict[str, str] = {}
    bare: list[str] = []
    for seg in rest.split():
        if "=" in seg:
            k, _, v = seg.partition("=")
            if k in keys and v:
                creds[k] = v
                continue
        bare.append(seg)
    if len(keys) == 1 and len(bare) == 1 and not creds:
        creds[keys[0]] = bare[0]
    elif bare:
        return None
    if set(creds) != set(keys):
        return None
    return creds


def _bind_guide(config: MCPConfig, config_id: str) -> str:
    keys = _bindable_keys(config)
    if not keys:
        return i18n_t("log.mcp.bind_guide_none", name=config.name)
    example_key = keys[0]
    url_line = f"\n{i18n_t('log.mcp.bind_guide_url', url=config.bind_url)}" if config.bind_url else ""
    hint = f"\n{config.bind_help}" if config.bind_help else ""
    return i18n_t(
        "log.mcp.bind_guide",
        name=config.name,
        keys="、".join(keys),
        example=f"mcp绑定 {config.name} {example_key}=<你的凭据>"
        if len(keys) > 1
        else f"mcp绑定 {config.name} <你的凭据>",
        help=url_line + hint,
        cid=config_id,
    )


def _is_private_chat(ev: Event) -> bool:
    return ev.user_type == "direct" or not ev.group_id


async def _verify_and_store(
    bot: Bot,
    ev: Event,
    config: MCPConfig,
    config_id: str,
    creds: dict[str, str],
) -> None:
    """验活（临时 client list_tools）→ 落库 → 掩码回执 → 清 @user: 缓存。"""
    from gsuid_core.ai_core.mcp.client import ensure_checkout_for_config

    await ensure_checkout_for_config(config)
    kwargs = render_client_kwargs(config, creds)
    client = MCPClient(
        name=config.name,
        transport=config.get_transport(),
        command=kwargs["command"],
        args=kwargs["args"],
        env=kwargs["env"],
        url=kwargs["url"],
        headers=kwargs["headers"],
        cwd=config.cwd,
    )
    try:
        await client.list_tools()
    except Exception:
        await bot.send(i18n_t("log.mcp.bind_verify_fail", name=config.name))
        return

    now = time.time()
    await MCPUserCredential.upsert_row(
        config_id,
        ev.bot_id,
        ev.user_id,
        creds,
        bound_via="chat",
        bound_at=now,
        last_ok_at=now,
    )
    from .startup import drop_user_client_cache

    drop_user_client_cache(config_id, ev.bot_id, ev.user_id)
    masked = "、".join(f"{k}=…{mask_token_tail(v)[-4:]}" for k, v in creds.items())
    await bot.send(i18n_t("log.mcp.bind_ok", name=config.name, masked=masked))


@sv_mcp_bind.on_command("mcp绑定", block=True)
async def mcp_bind_cmd(bot: Bot, ev: Event) -> None:
    if not _is_private_chat(ev):
        await bot.send(i18n_t("log.mcp.bind_private_only"))
        return

    rest = ev.text.strip() if ev.text else ""
    if not rest:
        # 无参：列出可绑服务 + 状态
        lines: list[str] = []
        for cid, cfg in mcp_config_manager.list_config_items():
            if not cfg.enabled:
                continue
            keys = _bindable_keys(cfg)
            if not keys:
                continue
            row = await MCPUserCredential.get_row(cid, ev.bot_id, ev.user_id)
            status = "✅已绑定" if row is not None and row.status == "ok" else "⬜未绑定"
            lines.append(f"• {cfg.name}（{cid}）— {status}，需绑：{'、'.join(keys)}")
        if not lines:
            await bot.send(i18n_t("log.mcp.bind_list_empty"))
        else:
            await bot.send(i18n_t("log.mcp.bind_available", lines="\n".join(lines)))
        return

    config, config_id, args_str = _resolve_config(rest)
    if config is None or config_id is None:
        await bot.send(i18n_t("log.mcp.bind_no_service", query=rest))
        return

    keys = _bindable_keys(config)
    if not args_str:
        await bot.send(_bind_guide(config, config_id))
        return

    creds = _parse_bind_args(args_str, keys)
    if creds is None:
        await bot.send(_bind_guide(config, config_id))
        return
    creds = sanitize_creds(creds)
    await _verify_and_store(bot, ev, config, config_id, creds)


@sv_mcp_bind.on_command("mcp解绑", block=True)
async def mcp_unbind_cmd(bot: Bot, ev: Event) -> None:
    if not _is_private_chat(ev):
        await bot.send(i18n_t("log.mcp.bind_private_only"))
        return
    rest = ev.text.strip() if ev.text else ""
    if not rest:
        await bot.send(i18n_t("log.mcp.unbind_usage"))
        return
    config, config_id, _ = _resolve_config(rest)
    if config_id is None:
        await bot.send(i18n_t("log.mcp.bind_no_service", query=rest))
        return
    ok = await MCPUserCredential.delete_row(config_id, ev.bot_id, ev.user_id)
    from .startup import drop_user_client_cache

    drop_user_client_cache(config_id, ev.bot_id, ev.user_id)
    if ok:
        assert config is not None
        await bot.send(i18n_t("log.mcp.unbind_ok", name=config.name))
    else:
        await bot.send(i18n_t("log.mcp.unbind_none"))


@sv_mcp_bind.on_command("mcp绑定列表", block=True)
async def mcp_bind_list_cmd(bot: Bot, ev: Event) -> None:
    if not _is_private_chat(ev):
        await bot.send(i18n_t("log.mcp.bind_private_only"))
        return
    rows = await MCPUserCredential.list_rows_for_user(ev.bot_id, ev.user_id)
    if not rows:
        await bot.send(i18n_t("log.mcp.bind_list_empty"))
        return
    lines = []
    for row in rows:
        cfg = mcp_config_manager.get_config(row.config_id)
        name = cfg.name if cfg is not None else row.config_id
        masked = masked_summary(row)
        masked_str = "、".join(f"{k}={v}" for k, v in masked.items()) or "-"
        bound = time.strftime("%Y-%m-%d", time.localtime(row.bound_at)) if row.bound_at else "-"
        lines.append(f"• {name} — {masked_str}（{bound}，{row.status}）")
    await bot.send(i18n_t("log.mcp.bind_list", lines="\n".join(lines)))


def bind_guide_for_tool(config_id: str) -> str:
    """wrapper 用：未绑/过期时回给 agent 的 BIND_GUIDE 文本。"""
    config = mcp_config_manager.get_config(config_id)
    if config is None:
        return i18n_t("log.mcp.bind_guide_missing")
    return _bind_guide(config, config_id)


try:
    from gsuid_core.ai_core.register import ai_tools

    @ai_tools(category="default", code_callable=False)
    async def mcp_bind_guide(service_name: str) -> str:
        """获取 MCP 服务的凭据绑定指引。当 MCP 工具返回需要绑定凭据时调用，
        返回告诉用户如何私聊完成绑定的说明。本工具只回指引文本，绝不接收或回传凭据。

        Args:
            service_name: MCP 服务名称（如「瑞幸」「Luckin Coffee」）
        """
        config, config_id, _ = _resolve_config(service_name)
        if config_id is None or config is None:
            return f"未找到 MCP 服务「{service_name}」。"
        return _bind_guide(config, config_id)

except ImportError:
    pass


def log_bind_attempt(command: str, ev: Event, outcome: str) -> None:
    """凭据命令审计：只记动词/服务/结果，永不记 token。"""
    logger.info(
        i18n_t(
            "log.mcp.bind_audit",
            cmd=command,
            uid=ev.user_id,
            bot=ev.bot_id,
            outcome=outcome,
        )
    )
