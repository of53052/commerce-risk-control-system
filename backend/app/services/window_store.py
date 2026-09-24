"""事件条带读写（Redis ZSET）—— 特征计算的唯一数据入口。

条带结构（docs/ARCHITECTURE.md §6.2）::

    KEY    rc:evt:{entity}:{entity_id}:{event_type}:{window}
    MEMBER {ts_ms}|{event_id}|{amount}
    SCORE  ts_ms
    TTL    窗口秒数 + 10 分钟

为什么不拆成"计数器 + 金额累加器"两个键：
    1. member 里带 ``event_id``，同一事件重复写入 ZSET 是幂等的（集合语义免费去重），
       而两个独立的 INCR 会在重试时重复累加 —— 这是最实际的理由。
    2. 一次 ``ZRANGEBYSCORE`` 就能同时拿到"条数"和"金额"，少一次往返。
    3. 窗口滑动无需额外逻辑：查询时按 ``[now - window, now]`` 取范围，
       过期数据由 TTL 自然淘汰。

**为什么用事件自身时间戳而不是服务器当前时间**：
    历史数据重放（``rebuild_windows.py``、场景回放）时，
    若用服务器时间做窗口边界，重放结果与当时实时决策结果会不一致，
    "可复现"这条架构目标就废了（docs/ARCHITECTURE.md §7.1 边界处理）。
    因此所有读写都以调用方传入的 ``now_ms`` 为基准。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError

from app.db.redis_client import evt_key, evt_ttl, get_redis

# 实体 -> 事件的归因字段。名称与 rc_event 的列名一致，便于直接从事件对象取值。
ENTITY_USER = "user"
ENTITY_DEVICE = "device"
ENTITY_IP = "ip"
ENTITY_ADDRESS = "address"
ENTITY_PHONE = "phone"

ALL_ENTITIES = (ENTITY_USER, ENTITY_DEVICE, ENTITY_IP, ENTITY_ADDRESS, ENTITY_PHONE)


@dataclass(frozen=True)
class StripMember:
    """条带成员解析结果。"""

    ts_ms: int
    event_id: str
    amount: float


def pack_member(ts_ms: int, event_id: str, amount: float | None = None) -> str:
    """打包成员字符串。

    ``amount`` 缺省写 0：保持字段数固定（永远三段），
    解析侧就不必写"两段/三段"两套分支。分隔符用 ``|``，
    而业务号里不含 ``|``（订单号/事件号都由字母数字下划线短横组成）。
    """
    return f"{ts_ms}|{event_id}|{amount if amount is not None else 0}"


def unpack_member(member: str) -> StripMember:
    """解析成员字符串；格式异常时返回 amount=0 而不抛错。

    容错理由：条带是"观测数据"，一条脏成员不应该让整次特征计算失败；
    计数仍然有效，只有金额这一项退化为 0。
    """
    parts = member.split("|")
    ts_ms = int(parts[0]) if parts and parts[0].isdigit() else 0
    event_id = parts[1] if len(parts) > 1 else ""
    amount = 0.0
    if len(parts) > 2:
        try:
            amount = float(parts[2])
        except ValueError:
            amount = 0.0
    return StripMember(ts_ms=ts_ms, event_id=event_id, amount=amount)


def add_event(
    *,
    entity: str,
    entity_id: str,
    event_type: str,
    window: str,
    window_seconds: int,
    ts_ms: int,
    event_id: str,
    amount: float | None = None,
    client: Redis | None = None,
) -> None:
    """把一个事件写入一条条带。"""
    redis = client or get_redis()
    key = evt_key(entity, entity_id, event_type, window)
    member = pack_member(ts_ms, event_id, amount)
    pipe = redis.pipeline(transaction=False)
    pipe.zadd(key, {member: ts_ms})
    # 每次写都续 TTL：条带 TTL 的语义是"最后一条事件之后还留多久"，
    # 而不是"键创建之后多久过期"，否则活跃实体的条带会在窗口中途消失。
    pipe.expire(key, evt_ttl(window_seconds))
    pipe.execute()


def add_event_to_entities(
    *,
    entities: Iterable[tuple[str, str]],
    event_type: str,
    windows: dict[str, int],
    ts_ms: int,
    event_id: str,
    amount: float | None = None,
    client: Redis | None = None,
) -> int:
    """一次把事件写入「多个实体 × 多个窗口」的全部条带。

    用 pipeline 批量下发：一次决策要写 5 个实体 × 3 个窗口 = 15 个键，
    不批量化就是 15 次 RTT，在本地也能占掉几十毫秒。
    返回实际下发的命令数（便于测试断言与排障）。
    """
    redis = client or get_redis()
    member = pack_member(ts_ms, event_id, amount)
    pipe = redis.pipeline(transaction=False)
    commands = 0

    for entity, entity_id in entities:
        if not entity_id:
            # 实体字段为空（如事件无收货地址）：不建键。
            # 该组特征在读取时会因为键不存在而返回 0，并记入 missing_fields。
            continue
        for window, seconds in windows.items():
            key = evt_key(entity, entity_id, event_type, window)
            pipe.zadd(key, {member: ts_ms})
            pipe.expire(key, evt_ttl(seconds))
            commands += 2

    if commands:
        pipe.execute()
    return commands


def count(
    *,
    entity: str,
    entity_id: str,
    event_type: str,
    window: str,
    window_seconds: int,
    now_ms: int,
    client: Redis | None = None,
) -> int:
    """窗口内事件条数（条带成员去重，天然不重复计数）。"""
    redis = client or get_redis()
    key = evt_key(entity, entity_id, event_type, window)
    return int(redis.zcount(key, now_ms - window_seconds * 1000, now_ms))


def range_members(
    *,
    entity: str,
    entity_id: str,
    event_type: str,
    window: str,
    window_seconds: int,
    now_ms: int,
    client: Redis | None = None,
) -> list[StripMember]:
    """窗口内的条带成员（按时间升序）。"""
    redis = client or get_redis()
    key = evt_key(entity, entity_id, event_type, window)
    raw = redis.zrangebyscore(key, now_ms - window_seconds * 1000, now_ms)
    return [unpack_member(item) for item in raw]


def sum_amount(
    *,
    entity: str,
    entity_id: str,
    event_type: str,
    window: str,
    window_seconds: int,
    now_ms: int,
    client: Redis | None = None,
) -> float:
    """窗口内金额合计。

    金额精度：条带里存的是 str 转 float，求和会有浮点误差；
    上游金额字段用 ``Numeric`` 存库、条带只用于"风险判断量级"，
    因此这里保留 float 并统一在输出时 ``round(x, 2)``，
    不引入 Decimal 的额外开销与复杂度。
    """
    members = range_members(
        entity=entity,
        entity_id=entity_id,
        event_type=event_type,
        window=window,
        window_seconds=window_seconds,
        now_ms=now_ms,
        client=client,
    )
    return round(sum(member.amount for member in members), 2)


def distinct_count(
    *,
    entity: str,
    entity_id: str,
    event_type: str,
    window: str,
    window_seconds: int,
    now_ms: int,
    prefix: str,
    client: Redis | None = None,
) -> int:
    """窗口内「去重后的不同主体数」。

    实现方式：把条带成员拉出来，从 member 的第三段读不到区分信息，
    因此聚簇类特征（同设备关联账号数、同 IP 关联账号数）**单靠事件条带做不到**。
    这里改为约定：这类特征使用**带主体前缀的条带** —— 写入时把
    ``event_id`` 位写成 ``{prefix}:{subject_id}``（见 feature_engine 的聚簇特征），
    读取时按前缀去重。这样仍是一个 ZSET 搞定，不需要额外的 SET 结构。
    """
    members = range_members(
        entity=entity,
        entity_id=entity_id,
        event_type=event_type,
        window=window,
        window_seconds=window_seconds,
        now_ms=now_ms,
        client=client,
    )
    subjects = {
        member.event_id.split(":", 1)[1]
        for member in members
        if member.event_id.startswith(f"{prefix}:")
    }
    return len(subjects)


def safe_call(default, func, *args, **kwargs):
    """Redis 不可用时的降级包装。

    风控系统对 Redis 的依赖是"性能与准确度"，不是"可用性"：
    Redis 挂了应降级为"特征缺失"（规则判 false），而不是让事件接入整体 500。
    降级事实通过返回值体现，由上层写入 ``missing_fields`` 并留痕。
    """
    try:
        return func(*args, **kwargs)
    except RedisError:
        return default

