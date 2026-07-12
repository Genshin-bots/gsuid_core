"""
WebConsole 登录会话存储

- 会话持久化到 data/webconsole_sessions.json：后端重启后已登录用户无需重新输入账密
- 令牌有效期 48 小时（TOKEN_TTL_HOURS），从登录时刻起算，与后端是否重启无关
- 同账号并发会话数由核心配置 ``web_max_sessions`` 控制（默认 1 = 单点登录）：
  新登录成功后，同账号超出限制的最旧会话会被踢下线（其下次请求返回 401）
- 磁盘中只保存 sha256(令牌) 摘要：拿到会话文件本身无法还原令牌伪造请求
"""

import json
import hashlib
import secrets
from typing import Any, Dict, Optional, TypedDict
from datetime import datetime, timedelta

from boltons.fileutils import atomic_save

from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.data_store import WEB_SESSIONS_PATH

# 登录有效期（小时）
TOKEN_TTL_HOURS = 48
# 并发会话数护栏：防止误配成 0（把自己锁死）或天文数字
_MAX_SESSIONS_FLOOR = 1
_MAX_SESSIONS_CEIL = 100


class SessionUser(TypedDict):
    """会话中缓存的用户信息（登录成功时从 WebUser 快照而来）"""

    id: str
    email: str
    name: str
    role: str
    avatar: Optional[str]


class SessionRecord(TypedDict):
    """单条会话记录；created/expires 为 datetime.isoformat() 字符串"""

    user: Dict[str, Any]
    email: str
    created: str
    expires: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str) -> Optional[datetime]:
    # 会话文件允许被人工编辑，时间字符串按外部输入对待，解析失败视作记录损坏
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _coerce_record(raw: object) -> Optional[SessionRecord]:
    """把磁盘读出的未知结构校验收敛为 SessionRecord；不合法返回 None（丢弃）。"""
    if not isinstance(raw, dict):
        return None
    for field in ("user", "email", "created", "expires"):
        if field not in raw:
            return None
    user = raw["user"]
    email = raw["email"]
    created = raw["created"]
    expires = raw["expires"]
    if not isinstance(user, dict):
        return None
    if not (isinstance(email, str) and isinstance(created, str) and isinstance(expires, str)):
        return None
    return SessionRecord(user=user, email=email, created=created, expires=expires)


def max_sessions_per_user() -> int:
    """同账号最大并发会话数（core_config: web_max_sessions，1 = 单点登录）。"""
    value = core_config.get_config("web_max_sessions")
    # config.json 可被人工编辑，运行时仍需 isinstance 守卫（bool 是 int 子类，需排除）
    if not isinstance(value, int) or isinstance(value, bool):
        return _MAX_SESSIONS_FLOOR
    return max(_MAX_SESSIONS_FLOOR, min(_MAX_SESSIONS_CEIL, value))


class SessionStore:
    """文件持久化的登录会话表：{sha256(token): SessionRecord}"""

    def __init__(self) -> None:
        self._sessions: Dict[str, SessionRecord] = {}
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        if not WEB_SESSIONS_PATH.exists():
            return
        # 会话文件属外部输入（可能被人工编辑/损坏），解析失败按空会话表处理并告警
        try:
            with open(WEB_SESSIONS_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(t("[网页控制台] 会话文件损坏, 已忽略: {e}", e=e))
            return
        if not isinstance(raw, dict):
            return
        now = datetime.now()
        for key, item in raw.items():
            record = _coerce_record(item)
            if record is None or not isinstance(key, str):
                continue
            expires = _parse_iso(record["expires"])
            if expires is not None and now < expires:
                self._sessions[key] = record

    def _save(self) -> None:
        # 保存失败只影响「重启后恢复会话」，不应让登录/登出本身报错，记警告即可
        try:
            with atomic_save(
                str(WEB_SESSIONS_PATH),
                text_mode=False,
                overwrite=True,
                file_perms=0o600,
            ) as f:
                if f:
                    f.write(json.dumps(self._sessions, indent=2, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            logger.warning(t("[网页控制台] 会话文件写入失败: {e}", e=e))

    # ---------- 会话生命周期 ----------

    def create(self, user: SessionUser) -> str:
        """登录成功后创建会话，返回明文令牌（仅此一次可见，磁盘只存摘要）。

        同时执行同账号并发数限制：超额时最旧的会话被踢下线。
        """
        token = secrets.token_urlsafe(32)
        now = datetime.now()
        self._sessions[_hash_token(token)] = SessionRecord(
            user=dict(user),
            email=user["email"],
            created=now.isoformat(),
            expires=(now + timedelta(hours=TOKEN_TTL_HOURS)).isoformat(),
        )
        self._evict_over_limit(user["email"])
        self._prune_expired()
        self._save()
        return token

    def verify(self, token: str) -> Optional[SessionRecord]:
        """校验令牌：有效返回会话记录（含 user 字段），过期/不存在返回 None。"""
        key = _hash_token(token)
        if key not in self._sessions:
            return None
        record = self._sessions[key]
        expires = _parse_iso(record["expires"])
        if expires is None or datetime.now() >= expires:
            del self._sessions[key]
            self._save()
            return None
        return record

    def revoke(self, token: str) -> None:
        """登出：立即失效该令牌。"""
        key = _hash_token(token)
        if key in self._sessions:
            del self._sessions[key]
            self._save()

    def update_user_fields(self, email: str, **fields: Any) -> None:
        """同步该账号所有在线会话中缓存的用户信息（改名/换头像后调用）。"""
        changed = False
        for record in self._sessions.values():
            if record["email"] == email:
                record["user"].update(fields)
                changed = True
        if changed:
            self._save()

    # ---------- 内部维护 ----------

    def _evict_over_limit(self, email: str) -> None:
        limit = max_sessions_per_user()
        mine = [(key, record) for key, record in self._sessions.items() if record["email"] == email]
        if len(mine) <= limit:
            return
        # 按创建时间从旧到新，踢掉最旧的超额会话
        mine.sort(key=lambda kv: kv[1]["created"])
        for key, _record in mine[: len(mine) - limit]:
            del self._sessions[key]

    def _prune_expired(self) -> None:
        now = datetime.now()
        expired = [
            key
            for key, record in self._sessions.items()
            if (exp := _parse_iso(record["expires"])) is None or now >= exp
        ]
        for key in expired:
            del self._sessions[key]


session_store: SessionStore = SessionStore()
