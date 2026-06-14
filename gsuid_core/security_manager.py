import time
from typing import Any, List, Deque, Tuple, DefaultDict
from collections import deque, defaultdict
from dataclasses import dataclass

from gsuid_core.config import core_config

TRUSTED_IPS: List[str] = core_config.get_config("TRUSTED_IPS")


def get_client_ip(request: Any) -> str:
    """提取客户端真实 IP（用于限流 / 审计）。

    仅当直连来源位于 TRUSTED_IPS（受信任的反向代理 / 本机）时，才信任
    ``X-Forwarded-For`` / ``X-Real-IP`` 请求头，避免攻击者伪造请求头绕过限流。
    """
    client = getattr(request, "client", None)
    client_host = getattr(client, "host", None) or "unknown"

    if client_host in TRUSTED_IPS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # 取最左侧（最初发起请求的客户端）地址
            return forwarded.split(",")[0].strip() or client_host
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip() or client_host
    return client_host


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
        """判断是否为信任地址"""
        return ip in TRUSTED_IPS

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
