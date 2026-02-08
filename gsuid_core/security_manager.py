import time
from typing import List
from collections import defaultdict
from dataclasses import dataclass

from gsuid_core.config import core_config

TRUSTED_IPS: List[str] = core_config.get_config("TRUSTED_IPS")


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
