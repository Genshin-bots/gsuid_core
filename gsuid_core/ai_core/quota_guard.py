"""Provider 配额 / 限流 / 过载的分级与熔断。

provider 把三类故障都塞进 4xx/5xx，但对 Agent 的处置完全不同：

===============  ==========================  ================================
类别             特征                        处置
===============  ==========================  ================================
``quota``        套餐用量打满（重试必复现）  fail-fast + 开闸，不烧重试预算
``rate_limit``   速率限制（短窗可恢复）      按原有退避重试
``overloaded``   集群过载（真瞬时）        按原有退避重试
===============  ==========================  ================================

``quota`` 命中后按「上游模型配置」开闸（键取本轮激活的配置全名）：闸内直接短路，
避免「同一句兜底在群里连喷 N 次」。判据只读 provider 返回体里的**错误码 / 关键词**，
不绑具体 provider、不含业务域词。
"""

from __future__ import annotations

import re
import time
from typing import Final, Literal

QuotaKind = Literal["quota", "rate_limit", "overloaded", "other"]

#: provider 错误体里的「用量/配额打满」特征。与 const._RETRYABLE_4XX 的区别：
#: 那个只问「状态码可不可重试」，这里问「重试有没有意义」。
_QUOTA_CODES: Final[tuple[str, ...]] = ("2056",)
_QUOTA_WORDS: Final[tuple[str, ...]] = (
    "用量上限",
    "配额",
    "额度已用尽",
    "quota exceeded",
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
)
_RATE_LIMIT_CODES: Final[tuple[str, ...]] = ("2062",)
_OVERLOAD_CODES: Final[tuple[str, ...]] = ("2064",)

#: 闸门默认开多久（秒）。套餐打满通常要人工处理，短窗重试只是烧钱。
DEFAULT_QUOTA_BREAK_SECONDS: Final[float] = 900.0
#: 同一会话多久内不重复下发同一句兜底（秒）。
DEFAULT_NOTIFY_SUPPRESS_SECONDS: Final[float] = 300.0


def _blob(exc: BaseException) -> str:
    """provider 异常的可搜索文本。``ModelHTTPError.body`` 静态是 Any，就地收窄成 str。"""
    from pydantic_ai.exceptions import ModelHTTPError

    if isinstance(exc, ModelHTTPError):
        body = exc.body
        return f"{body if isinstance(body, str) else str(body)} {exc}".lower()
    return str(exc).lower()


def classify_provider_error(exc: BaseException) -> QuotaKind:
    """把 provider 异常归到四类之一。判据只读错误码与关键词。"""
    from pydantic_ai.exceptions import ModelHTTPError

    if not isinstance(exc, ModelHTTPError):
        return "other"
    blob = _blob(exc)
    if _has_code(blob, _QUOTA_CODES) or any(w in blob for w in _QUOTA_WORDS):
        return "quota"
    if _has_code(blob, _RATE_LIMIT_CODES):
        return "rate_limit"
    if _has_code(blob, _OVERLOAD_CODES):
        return "overloaded"
    return "other"


def _has_code(blob: str, codes: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{code}\b", blob) for code in codes)


class QuotaBreaker:
    """单个上游配置的配额熔断器 + 兜底文案去重。进程内存态，多实例不共享。"""

    def __init__(self) -> None:
        self._opened_at: dict[str, float] = {}
        self._notified_at: dict[str, float] = {}
        self._break_seconds = DEFAULT_QUOTA_BREAK_SECONDS
        self._notify_seconds = DEFAULT_NOTIFY_SUPPRESS_SECONDS

    def configure(self, *, break_seconds: float, notify_seconds: float) -> None:
        self._break_seconds = max(0.0, break_seconds)
        self._notify_seconds = max(0.0, notify_seconds)
        self._expire()

    def _expire(self, now: float | None = None) -> None:
        # session 去重键只增不减；读路径顺手清过期项，避免长跑进程单调堆积。
        now = time.monotonic() if now is None else now
        self._drop_expired(self._opened_at, self._break_seconds, now)
        self._drop_expired(self._notified_at, self._notify_seconds, now)

    @staticmethod
    def _drop_expired(store: dict[str, float], window: float, now: float) -> None:
        stale = [key for key, ts in store.items() if now - ts >= window]
        for key in stale:
            del store[key]

    def note_hit(self, key: str, kind: QuotaKind) -> None:
        """记录一次 provider 故障。``quota`` 立刻开闸。"""
        self._expire()
        if kind == "quota":
            self._opened_at[key] = time.monotonic()

    def is_open(self, key: str) -> bool:
        """该上游的闸是否仍开着（开着就别再试了）。"""
        self._expire()
        return key in self._opened_at

    def remaining_break_seconds(self, key: str) -> float:
        now = time.monotonic()
        self._expire(now)
        opened = self._opened_at.get(key)
        if opened is None:
            return 0.0
        return max(0.0, self._break_seconds - (now - opened))

    def should_notify(self, key: str) -> bool:
        """闸内重复失败时只放行第一条兜底，其余静默，避免同句连喷。"""
        now = time.monotonic()
        self._expire(now)
        last = self._notified_at.get(key)
        if last is not None and now - last < self._notify_seconds:
            return False
        self._notified_at[key] = now
        return True

    def reset(self, key: str) -> None:
        self._opened_at.pop(key, None)
        self._notified_at.pop(key, None)

    def clear(self) -> None:
        self._opened_at.clear()
        self._notified_at.clear()


quota_breaker = QuotaBreaker()
