"""MCP 用户凭据存储。

单表 ``MCPUserCredential``（Bind 子类）：逻辑主键
``(config_id, bot_id, user_id)``；``creds_json`` 为 Fernet 密文
（密钥源 = core_config.json 的 ``REGISTER_CODE``，派生 ``b64(sha256(code))``）。

查取顺序：精确行唯一——无行返回 None（调用方产 BIND_GUIDE），
**永不回落共享凭据**。``bound_via`` 区分 ``chat``（用户自绑）与
``admin_install``（装机时为订阅表主人批量落行，值=共享 token 快照）。

``render_client_kwargs`` 把 creds map 套进 MCPClient 构造参数：
env 覆盖 + headers/command/args 里 env 值的字符串替换（非空才替换，
短值跳过防误伤）。过期/吊销/重绑/删配置 → 清对应 ``@user:`` 缓存。
"""

import json
import base64
import hashlib
from typing import Any, Dict, List, Tuple, Optional

from sqlmodel import Field, col, select
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.config import core_config
from gsuid_core.utils.database.base_models import Bind, with_session

from .pii import has_credential_form
from .config_manager import MCPConfig, mcp_config_manager


def _fernet():
    """Fernet 实例：密钥 = base64(sha256(REGISTER_CODE))。"""
    from cryptography.fernet import Fernet

    code = core_config.get_config("REGISTER_CODE")
    if not isinstance(code, str) or not code:
        raise RuntimeError("REGISTER_CODE 未配置，无法加解密 MCP 用户凭据")
    key = base64.urlsafe_b64encode(hashlib.sha256(code.encode()).digest())
    return Fernet(key)


def _encrypt(creds: Dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(creds, ensure_ascii=False).encode()).decode()


def _decrypt(creds_json: str) -> Optional[Dict[str, str]]:
    try:
        raw = _fernet().decrypt(creds_json.encode()).decode()
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


def mask_token_tail(token: str) -> str:
    """凭据回执掩码：只露末 4 位，短串全掩。"""
    if len(token) <= 4:
        return "****"
    return f"****{token[-4:]}"


def sanitize_creds(creds: Dict[str, str]) -> Dict[str, str]:
    """绑定入参清洗：拒含空白/过短/非凭据形态的值。"""
    return {k.strip(): v.strip() for k, v in creds.items() if k.strip() and v.strip()}


_table_ensured = False


async def _select_row(
    session: AsyncSession,
    cls: type["MCPUserCredential"],
    config_id: str,
    bot_id: str,
    user_id: str,
) -> Optional["MCPUserCredential"]:
    stmt = select(cls).where(
        col(cls.config_id) == config_id,
        col(cls.bot_id) == bot_id,
        col(cls.user_id) == user_id,
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    return row if isinstance(row, cls) else None


class MCPUserCredential(Bind, table=True):
    """某用户在某 bot 平台上对某 MCP 服务的一组绑定凭据。"""

    config_id: str = Field(default="", index=True, title="MCP 配置 ID")
    creds_json: str = Field(default="", title="Fernet 密文 creds map")
    bound_via: str = Field(default="chat", title="绑定来源：chat / admin_install")
    bound_at: float = Field(default=0.0, title="绑定时间戳")
    last_ok_at: float = Field(default=0.0, title="最近一次调用成功时间戳")
    status: str = Field(default="ok", title="ok / expired / revoked")

    @classmethod
    async def ensure_table(cls) -> None:
        """懒建表：全局 create_all 时序早于本模块 import（见 state_store）。"""
        global _table_ensured
        if _table_ensured:
            return
        from gsuid_core.i18n import t as i18n_t
        from gsuid_core.logger import logger
        from gsuid_core.utils.database.base_models import engine

        try:
            async with engine.begin() as conn:
                await conn.run_sync(
                    cls.metadata.create_all,
                    tables=[cls.metadata.tables["mcpusercredential"]],
                    checkfirst=True,
                )
        except Exception as e:
            logger.warning(i18n_t("log.mcp.cred_table_check_fail", e=e))
        _table_ensured = True

    @classmethod
    @with_session
    async def get_row(
        cls,
        session: AsyncSession,
        config_id: str,
        bot_id: str,
        user_id: str,
    ) -> Optional["MCPUserCredential"]:
        await cls.ensure_table()
        return await _select_row(session, cls, config_id, bot_id, user_id)

    @classmethod
    @with_session
    async def list_rows_for_user(
        cls,
        session: AsyncSession,
        bot_id: str,
        user_id: str,
    ) -> List["MCPUserCredential"]:
        await cls.ensure_table()
        stmt = select(cls).where(col(cls.bot_id) == bot_id, col(cls.user_id) == user_id)
        result = await session.execute(stmt)
        return [r for r in result.scalars().all() if isinstance(r, cls)]

    @classmethod
    @with_session
    async def list_rows_for_config(
        cls,
        session: AsyncSession,
        config_id: str,
    ) -> List["MCPUserCredential"]:
        await cls.ensure_table()
        stmt = select(cls).where(col(cls.config_id) == config_id)
        result = await session.execute(stmt)
        return [r for r in result.scalars().all() if isinstance(r, cls)]

    @classmethod
    @with_session
    async def upsert_row(
        cls,
        session: AsyncSession,
        config_id: str,
        bot_id: str,
        user_id: str,
        creds: Dict[str, str],
        *,
        bound_via: str,
        bound_at: float,
        last_ok_at: float,
    ) -> "MCPUserCredential":
        row = await _select_row(session, cls, config_id, bot_id, user_id)
        if row is None:
            row = cls(
                config_id=config_id,
                bot_id=bot_id,
                user_id=user_id,
                group_id=None,
            )
        row.creds_json = _encrypt(creds)
        row.bound_via = bound_via
        row.bound_at = bound_at
        row.last_ok_at = last_ok_at
        row.status = "ok"
        session.add(row)
        return row

    @classmethod
    @with_session
    async def delete_row(
        cls,
        session: AsyncSession,
        config_id: str,
        bot_id: str,
        user_id: str,
    ) -> bool:
        row = await _select_row(session, cls, config_id, bot_id, user_id)
        if row is None:
            return False
        await session.delete(row)
        return True

    @classmethod
    @with_session
    async def mark_expired(
        cls,
        session: AsyncSession,
        config_id: str,
        bot_id: str,
        user_id: str,
    ) -> None:
        row = await _select_row(session, cls, config_id, bot_id, user_id)
        if row is not None:
            row.status = "expired"
            session.add(row)

    @classmethod
    @with_session
    async def delete_rows_for_config(
        cls,
        session: AsyncSession,
        config_id: str,
    ) -> int:
        stmt = select(cls).where(col(cls.config_id) == config_id)
        result = await session.execute(stmt)
        rows = [r for r in result.scalars().all() if isinstance(r, cls)]
        for row in rows:
            await session.delete(row)
        return len(rows)


def get_creds(row: MCPUserCredential) -> Optional[Dict[str, str]]:
    """解密凭据行；status 非 ok 或解密失败 → None（调用方产 BIND_GUIDE）。"""
    if row.status != "ok" or not row.creds_json:
        return None
    return _decrypt(row.creds_json)


def masked_summary(row: MCPUserCredential) -> Dict[str, str]:
    """回执/列表用：每个 key 只露尾 4 位。解密失败返回空 dict。"""
    creds = _decrypt(row.creds_json) if row.creds_json else None
    if creds is None:
        return {}
    return {k: mask_token_tail(v) for k, v in creds.items()}


def render_client_kwargs(config: MCPConfig, creds: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """把 creds map 套进 MCPClient 构造参数。

    - env：逐 key 覆盖（值来自绑定）
    - headers/command/args/url：把 config.env 里对应值的出现处替换为绑定值
      （headers/url 存的可能是渲染后值或 {KEY} 占位；空 env 值/短值跳过防误伤）
    - url 需要渲染：市场远程变体的凭据可能占位在 query 参数里（如
      ``?API_KEY={API_KEY}``），绑定用户必须能替换为自己的 key
    """

    def _fill_shared(text: str) -> str:
        out = text
        for key, val in config.env.items():
            if val:
                out = out.replace("{" + key + "}", val)
        return out

    if not creds:
        return {
            "env": dict(config.env),
            "headers": {k: _fill_shared(v) for k, v in config.headers.items()},
            "command": _fill_shared(config.command),
            "args": [_fill_shared(a) for a in config.args],
            "url": _fill_shared(config.url),
        }

    env = dict(config.env)
    for key in config.user_env_keys:
        if key in creds:
            env[key] = creds[key]

    def _render(text: str) -> str:
        out = text
        for key in config.user_env_keys:
            bound = creds.get(key)
            if bound is None:
                continue
            shared_val = config.env.get(key, "")
            if shared_val and len(shared_val) >= 6 and shared_val in out:
                out = out.replace(shared_val, bound)
            # {KEY} 占位无条件替换（env 有值时文本仍可能存的是占位形式）
            out = out.replace("{" + key + "}", bound)
        # 未绑定的键回落共享 env 值（绑定用户混用共享凭据的场景）
        for key, val in config.env.items():
            if val:
                out = out.replace("{" + key + "}", val)
        return out

    headers = {k: _render(v) for k, v in config.headers.items()}
    args = [_render(a) for a in config.args]
    command = _render(config.command)
    url = _render(config.url)
    return {"env": env, "headers": headers, "command": command, "args": args, "url": url}


def looks_suspicious_token(value: str) -> bool:
    """自由文本兜网：整串像 token（长无空格含字母数字）。"""
    return len(value) >= 16 and " " not in value and has_credential_form(f"token={value}")


async def resolve_user_creds(
    config_id: str,
    bot_id: str,
    user_id: str,
) -> Tuple[Optional[Dict[str, str]], Optional[MCPUserCredential]]:
    """查取发起人的绑定凭据。无行/非 ok/解密失败 → (None, row|None)。"""
    row = await MCPUserCredential.get_row(config_id, bot_id, user_id)
    if row is None:
        return None, None
    return get_creds(row), row


__all__ = [
    "MCPUserCredential",
    "get_creds",
    "masked_summary",
    "mask_token_tail",
    "render_client_kwargs",
    "resolve_user_creds",
    "sanitize_creds",
    "looks_suspicious_token",
    "_decrypt",
    "_encrypt",
    "mcp_config_manager",
]
