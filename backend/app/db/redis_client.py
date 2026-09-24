"""Redis 客户端与键空间常量。

集中三件事，避免散落各处：
    1. **键模式**：所有键的拼法只在这里定义（docs/ARCHITECTURE.md §6.2），
       键名写错是最难排查的一类 bug —— 它不报错，只是永远读到 0。
    2. **客户端单例**：``redis.Redis`` 自带连接池，进程内复用一个实例即可。
       ``decode_responses=True`` 让返回值是 str 而非 bytes，业务代码不做解码。
    3. **键生成函数**：拼键逻辑收敛到函数里，键格式变更时只改一处。

为什么不用 Redis 的 ZSET 之外的结构：见 docs/ARCHITECTURE.md §6.2 的说明
（ZSET 同时支持按时间范围计数与遍历求和，且 member 自带去重语义）。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from redis import Redis

from app.core.config import settings

# ---- 键空间（docs/ARCHITECTURE.md §6.2）----

# 事件条带：rc:evt:{entity}:{id}:{eventType}:{window}
EVT_KEY = "rc:evt:{entity}:{entity_id}:{event_type}:{window}"

# 幂等结果：rc:idem:{event_id}
IDEM_KEY = "rc:idem:{event_id}"

# 名单缓存：rc:list:{dimension}:{list_type}
LIST_KEY = "rc:list:{dimension}:{list_type}"

# 实时计数桶：rc:cnt:{metric}:{bucket}
CNT_KEY = "rc:cnt:{metric}:{bucket}"

# 模型热切换信号
MODEL_ACTIVE_KEY = "rc:model:active"

# SSE 广播频道
SSE_CHANNEL = "rc:sse:events"

# TTL（秒）
IDEM_TTL = 24 * 3600
LIST_TTL = 60
CNT_TTL = 2 * 3600
# 事件条带 TTL = 窗口时长 + 10 分钟（给"窗口刚过但任务还在跑"留缓冲）
EVT_TTL_MARGIN = 600


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    """进程内共享的 Redis 客户端。

    ``socket_connect_timeout`` / ``socket_timeout`` 设为 2 秒：
    特征计算在决策热路径上，宁可快速失败并让决策降级（缺失特征），
    也不要让请求挂住几十秒把线程池耗干。
    """
    return Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


def evt_key(entity: str, entity_id: str, event_type: str, window: str) -> str:
    """拼事件条带键。"""
    return EVT_KEY.format(entity=entity, entity_id=entity_id, event_type=event_type, window=window)


def evt_scan_pattern(entity: str, entity_id: str) -> str:
    """按实体扫描全部条带键（用于冷启动重建与调试）。

    注意：线上热路径绝不要用 ``KEYS``/``SCAN`` 做特征计算 ——
    本函数只服务于运维脚本。
    """
    return EVT_KEY.format(entity=entity, entity_id=entity_id, event_type="*", window="*")


def idem_key(event_id: str) -> str:
    return IDEM_KEY.format(event_id=event_id)


def list_key(dimension: str, list_type: str) -> str:
    return LIST_KEY.format(dimension=dimension, list_type=list_type)


def cnt_key(metric: str, bucket: str) -> str:
    return CNT_KEY.format(metric=metric, bucket=bucket)


def evt_ttl(window_seconds: int) -> int:
    """事件条带 TTL：窗口时长 + 缓冲。"""
    return window_seconds + EVT_TTL_MARGIN


def ping() -> bool:
    """连通性探测；异常返回 False 而不是抛错（健康检查与降级路径要用）。"""
    try:
        return bool(get_redis().ping())
    except Exception:  # noqa: BLE001 - 探测函数必须吞异常
        return False


def info_version() -> str | None:
    """取 Redis 版本，仅用于健康检查展示。"""
    try:
        return get_redis().info("server").get("redis_version")
    except Exception:  # noqa: BLE001
        return None


def reset_cache() -> None:
    """清掉单例（测试夹具在切换 DB 后需要重新取客户端）。"""
    get_redis.cache_clear()


def raw_client_kwargs() -> dict[str, Any]:  # pragma: no cover - 调试用
    return {
        "host": settings.REDIS_HOST,
        "port": settings.REDIS_PORT,
        "db": settings.REDIS_DB,
    }

