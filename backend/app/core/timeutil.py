"""时间工具。

约定（docs/PRD.md §16）：数据库统一存 UTC，界面按 Asia/Shanghai 展示。
MySQL 的 DATETIME 不带时区信息，因此这里统一产出 **naive UTC**，
在读写两端都当作 UTC 处理，避免出现"存进去是本地时间、读出来当 UTC"这类
只有当跨时区或跨夏令时才暴露的隐性 bug。
"""

from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive，秒级精度足够风控场景）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def utcnow_ms() -> int:
    """当前 UTC 毫秒时间戳，用于 Redis ZSET 的 score 与事件条带 member。"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    """毫秒时间戳 -> naive UTC datetime。"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def to_ms(dt: datetime) -> int:
    """naive UTC datetime -> 毫秒时间戳（按 UTC 解释输入）。"""
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def window_start(window_seconds: int, now: datetime | None = None) -> datetime:
    """窗口起始时间 = 当前时间 - 窗口长度。"""
    return (now or utcnow()) - timedelta(seconds=window_seconds)
