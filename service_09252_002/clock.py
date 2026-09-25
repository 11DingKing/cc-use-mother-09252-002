"""可替换的时钟端口与时间工具。

所有持久化时间一律为 UTC ISO8601 文本；外部输入允许带任意时区偏移，
进入领域前统一归一到 UTC，保证跨时区比较与重启后一致性。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .errors import ValidationError


def to_utc(value: datetime) -> datetime:
    """把任意 datetime 归一到 UTC；naive 输入按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_instant(text: str) -> datetime:
    """解析 ISO8601 时间戳（支持 ``Z`` 与 ``±HH:MM`` 偏移），返回 UTC。"""
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("时间戳必须是非空字符串")
    normalized = text.strip()
    if normalized.endswith(("Z", "z")):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError(f"无效的时间格式: {text!r}") from exc
    return to_utc(parsed)


def format_instant(value: datetime) -> str:
    """以 UTC ISO8601 文本输出，供持久化与 API 响应使用。"""
    return to_utc(value).isoformat()


class Clock:
    """时钟端口：领域逻辑只依赖此接口获取当前时间。"""

    def now(self) -> datetime:  # pragma: no cover - 抽象方法
        raise NotImplementedError


class SystemClock(Clock):
    """生产时钟：返回当前 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class MutableClock(Clock):
    """测试时钟：可固定、可推进，用于复现跨时区与期限边界。"""

    def __init__(self, instant: datetime):
        self._instant = to_utc(instant)

    def set(self, instant: datetime) -> None:
        self._instant = to_utc(instant)

    def advance(self, **kwargs) -> None:
        self._instant = self._instant + timedelta(**kwargs)

    def now(self) -> datetime:
        return self._instant
