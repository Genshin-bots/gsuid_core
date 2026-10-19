import time
import ipaddress
from typing import Deque, Tuple, Protocol, DefaultDict
from collections import deque, defaultdict
from dataclasses import dataclass

from starlette.datastructures import Address, Headers

from gsuid_core.config import core_config


def normalize_ip(raw: str) -> str:
    """收成单一合法 IP。IPv4-mapped IPv6 收成 IPv4。非法地址返回空串。"""
    text = raw.strip()
    if not text:
        return ""
    try:
        parsed = ipaddress.ip_address(text)
    except ValueError:
        return ""
    if isinstance(parsed, ipaddress.IPv6Address):
        mapped = parsed.ipv4_mapped
        if mapped is not None:
            return str(mapped)
    return str(parsed)


def trusted_proxy_ips() -> set[str]:
    """当前配置里的 TRUSTED_IPS，归一化后的合法地址。"""
    raw = core_config.get_config("TRUSTED_IPS")
    found: set[str] = set()
    if not isinstance(raw, list):
        return found
    for item in raw:
        if not isinstance(item, str):
            continue
        normalized = normalize_ip(item)
        if normalized:
            found.add(normalized)
    return found


def _header_text(headers: Headers, name: str) -> str:
    if name not in headers:
        return ""
    return headers[name]


def _untrusted_from_right(header: str, trusted: set[str], peer: str) -> str:
    hops: list[str] = []
    for part in header.split(","):
        ip = normalize_ip(part)
        if ip:
            hops.append(ip)
    if peer:
        hops.append(peer)
    for hop in reversed(hops):
        if hop not in trusted:
            return hop
    return ""


def resolve_client_ip(peer: str, forwarded_for: str, real_ip: str) -> str:
    """直连不在信任列表时用直连地址。在列表中时从右向左找站外地址。"""
    peer_ip = normalize_ip(peer)
    if not peer_ip:
        peer_ip = "unknown"
    trusted = trusted_proxy_ips()
    if peer_ip not in trusted:
        return peer_ip
    chosen = _untrusted_from_right(forwarded_for, trusted, peer_ip)
    if chosen:
        return chosen
    chosen = _untrusted_from_right(real_ip, trusted, "")
    if chosen:
        return chosen
    return peer_ip


class _PeerConnection(Protocol):
    @property
    def client(self) -> Address | None: ...

    @property
    def headers(self) -> Headers: ...


def get_client_ip(connection: _PeerConnection) -> str:
    """网页控制台限流、登录审计、访问日志和协同连接共用的客户端 IP。"""
    client = connection.client
    peer = client.host if client is not None else ""
    return resolve_client_ip(
        peer,
        _header_text(connection.headers, "x-forwarded-for"),
        _header_text(connection.headers, "x-real-ip"),
    )


@dataclass
class IPStatus:
    failed_count: int = 0
    ban_until: float = 0


class SecurityManager:
    def __init__(self):
        # 记录 IP 失败次数和封禁时间
        self.status = defaultdict(IPStatus)
        self.MAX_RETRIES = 5
        self.BAN_DURATION = 900

    def is_trusted(self, ip: str) -> bool:
        """与 ``get_client_ip`` 共用当前 ``TRUSTED_IPS``。"""
        normalized = normalize_ip(ip)
        if not normalized:
            return False
        return normalized in trusted_proxy_ips()

    def is_banned(self, ip: str) -> bool:
        """检查 IP 是否处于封禁期"""
        if self.status[ip].ban_until > time.time():
            return True
        return False

    def record_failure(self, ip: str):
        """记录一次失败尝试"""
        self.status[ip].failed_count += 1
        if self.status[ip].failed_count >= self.MAX_RETRIES:
            self.status[ip].ban_until = time.time() + self.BAN_DURATION
            self.status[ip].failed_count = 0  # 重置计数，等待封禁结束后重新计算

    def record_success(self, ip: str):
        """验证成功，重置失败计数"""
        self.status[ip].failed_count = 0


sec_manager = SecurityManager()


class AuthRateLimiter:
    """Web 控制台认证接口（登录 / 注册 / 改密）限流器。

    采用「滑动窗口频率限制」+「连续失败封禁」两层防护，按 key（通常为客户端
    IP）隔离，用于缓解暴力破解与凭据填充（credential stuffing）攻击。

    本限流器与负责 WS 机器人连接安全的 ``SecurityManager`` 相互独立，避免 Web
    端的登录失误误封机器人连接。限流状态仅保存在内存中，进程重启后清空（对单
    进程 uvicorn 部署足够）。
    """

    def __init__(self) -> None:
        # key -> 最近请求时间戳队列（滑动窗口）
        self._attempts: DefaultDict[str, Deque[float]] = defaultdict(deque)
        # key -> 连续失败次数
        self._failures: DefaultDict[str, int] = defaultdict(int)
        # key -> 封禁到期时间戳
        self._ban_until: DefaultDict[str, float] = defaultdict(float)

        self.WINDOW = 60.0  # 滑动窗口长度（秒）
        self.MAX_ATTEMPTS = 10  # 窗口内允许的最大请求次数
        self.MAX_FAILURES = 5  # 触发封禁的连续失败次数
        self.BAN_DURATION = 900.0  # 封禁时长（秒）

    def check(self, key: str) -> Tuple[bool, int]:
        """检查本次请求是否允许。

        返回 ``(allowed, retry_after)``，``retry_after`` 为建议的重试等待秒数
        （仅在被限流时有意义）。本方法应在处理请求体之前调用。
        """
        now = time.time()

        ban_until = self._ban_until.get(key, 0.0)
        if ban_until > now:
            return False, int(ban_until - now) + 1

        dq = self._attempts[key]
        threshold = now - self.WINDOW
        while dq and dq[0] <= threshold:
            dq.popleft()

        if len(dq) >= self.MAX_ATTEMPTS:
            retry_after = int(dq[0] + self.WINDOW - now) + 1
            return False, max(retry_after, 1)

        dq.append(now)
        return True, 0

    def record_failure(self, key: str) -> int:
        """记录一次认证失败，连续失败达到阈值则触发封禁。

        返回封禁前的剩余可尝试次数（已封禁时为 0）。
        """
        self._failures[key] += 1
        if self._failures[key] >= self.MAX_FAILURES:
            self._ban_until[key] = time.time() + self.BAN_DURATION
            self._failures[key] = 0
            self._attempts.pop(key, None)
            return 0
        return self.MAX_FAILURES - self._failures[key]

    def record_success(self, key: str) -> None:
        """认证成功，清除该 key 的全部限流状态。"""
        self._failures.pop(key, None)
        self._ban_until.pop(key, None)
        self._attempts.pop(key, None)


auth_rate_limiter = AuthRateLimiter()
