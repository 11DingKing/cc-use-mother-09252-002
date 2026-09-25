"""可替换端口：时间源与标识生成器。

所有时间都以带时区的 UTC 瞬时表示，禁止朴素（naive）时间进入领域层，
以便跨时区比较与故障重放时得到稳定结果。测试可注入固定时钟与序列 ID。
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Protocol

UTC = dt.timezone.utc


def ensure_utc(moment: dt.datetime) -> dt.datetime:
    """将带时区的时间规范化为 UTC；拒绝朴素时间。"""
    if not isinstance(moment, dt.datetime):
        raise ValueError("期望 datetime 类型")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("时间必须携带时区偏移")
    return moment.astimezone(UTC)


def parse_instant(text: str) -> dt.datetime:
    """解析 ISO 8601 瞬时（必须含偏移，如 +08:00 或 Z），返回 UTC。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("时间必须为非空字符串")
    try:
        parsed = dt.datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(f"无法解析的时间: {text!r}") from exc
    return ensure_utc(parsed)


def format_instant(moment: dt.datetime) -> str:
    """以 UTC ISO 字符串输出，字典序即时间序。"""
    return ensure_utc(moment).isoformat()


class Clock(Protocol):
    """时间源端口。"""

    def now(self) -> dt.datetime: ...


class SystemClock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(UTC)


class FixedClock:
    """测试用时钟：可设定与推进。"""

    def __init__(self, moment: dt.datetime):
        self._moment = ensure_utc(moment)

    def now(self) -> dt.datetime:
        return self._moment

    def set(self, moment: dt.datetime) -> None:
        self._moment = ensure_utc(moment)

    def advance(self, **kwargs) -> None:
        self._moment = self._moment + dt.timedelta(**kwargs)


class IdGenerator(Protocol):
    """标识生成端口。"""

    def new_id(self, prefix: str) -> str: ...


class UuidIds:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIds:
    """测试用：按前缀递增，便于断言。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def new_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}_{n:06d}"
