"""名单服务：五维度 × 黑/白/灰 的匹配、优先级仲裁与缓存。

职责边界（docs/PRD.md §8.1）：
    - 黑名单命中 → 直接 Reject（跳过规则与模型）
    - 白名单命中 → 直接 Pass（跳过规则与模型）
    - 灰名单命中 → 不决定动作，转成加成特征参与规则与模型
    - 黑白同时命中 → 按 ``sys_config.list_conflict_policy`` 仲裁

缓存设计（docs/ARCHITECTURE.md §7.5）：
    名单匹配在**每次决策**都要做，若每次都查 5 维度 × 3 类型的 MySQL，
    单这一项就够把 P95 拖过 50ms。因此：
        1. 按 ``dimension:list_type`` 维度把整组名单缓存到 Redis HASH（TTL 60s）；
        2. 写入名单后主动 ``DEL`` 对应键（写后即失效，比"等 TTL 过期"体验好）；
        3. Redis 不可用时直接查库 —— 正确性优先于性能，绝不因为缓存挂了就放行。

**为什么"过期"是逻辑判断而不是物理删除**：``expire_at`` 到期后仍然保留记录，
因为它是审计证据（"这个 IP 曾经在名单里"是可追溯的事实）。匹配时跳过过期项即可。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import orjson
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.db.redis_client import LIST_TTL, get_redis, list_key
from app.models.rclist import (
    ALL_DIMENSIONS,
    LIST_BLACK,
    LIST_GRAY,
    LIST_WHITE,
    STATUS_ACTIVE,
    RcListEntry,
)
from app.services.config_service import get_str

logger = logging.getLogger("app.services.list_service")

# 每个维度对应的"事件信封取哪个字段"。与 rc_event 的列名保持一致。
DIMENSION_FIELD: dict[str, str] = {
    "user": "user_id",
    "phone": "phone",
    "ip": "ip",
    "device": "device_id",
    "address": "address_hash",
}

# 空名单的缓存占位字段名（不会与真实名单值冲突：真实值不会是双下划线包裹的形式）
EMPTY_SENTINEL = "__empty__"

# 名单类型 -> 加成特征的键名（灰名单专用）
GRAY_FLAG_KEYS: dict[str, str] = {
    "user": "subject_gray_flag",
    "phone": "phone_gray_flag",
    "ip": "ip_gray_flag",
    "device": "device_gray_flag",
    "address": "address_gray_flag",
}

# 名单服务会产出的全部特征键。
# 用途：
#   1. 规则字段白名单 —— 规则可以引用名单标记（如 subject_gray_flag == 1），
#      这些键不在特征注册表里（它们不是窗口聚合，而是名单匹配结果）；
#   2. 默认值填充 —— 未命中时给 0/False，让规则总能拿到确定值，
#      而不是"键不存在 → 条件判 false + missing_fields 告警"。
BLACKLIST_FLAG_KEY = "subject_blacklist"
WHITELIST_FLAG_KEY = "subject_whitelist"

FLAG_FEATURE_KEYS: tuple[str, ...] = (
    BLACKLIST_FLAG_KEY,
    WHITELIST_FLAG_KEY,
    *GRAY_FLAG_KEYS.values(),
)


@dataclass(frozen=True)
class ListHit:
    """一条名单命中记录。"""

    dimension: str
    list_type: str
    value: str
    priority: int
    reason: str | None
    entry_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "list_type": self.list_type,
            "value": self.value,
            "priority": self.priority,
            "reason": self.reason,
            "entry_id": self.entry_id,
        }


@dataclass
class ListMatchResult:
    """一次决策的名单匹配结论。"""

    decision: str | None = None          # black 命中 -> "Reject"；white 命中 -> "Pass"；其余 None
    decided_by_list: bool = False
    hits: list[ListHit] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    conflict: bool = False                # 黑白同时命中
    policy: str = "black_first"
    degraded: bool = False                # 缓存不可用而降级查库

    def hit_summary(self) -> list[dict[str, Any]]:
        return [hit.to_dict() for hit in self.hits]


def _load_from_db(db: Session, dimension: str, list_type: str, now) -> dict[str, ListHit]:
    """从数据库读某一维度的名单，返回 {value: ListHit}（已过滤失效项）。"""
    rows = (
        db.execute(
            select(RcListEntry).where(
                RcListEntry.dimension == dimension,
                RcListEntry.list_type == list_type,
                RcListEntry.status == STATUS_ACTIVE,
            )
        )
        .scalars()
        .all()
    )
    table: dict[str, ListHit] = {}
    for row in rows:
        # 过期 = 未命中，但记录保留（过期名单仍是审计证据）
        if row.expire_at is not None and row.expire_at <= now:
            continue
        table[row.value] = ListHit(
            dimension=row.dimension,
            list_type=row.list_type,
            value=row.value,
            priority=row.priority,
            reason=row.reason,
            entry_id=row.id,
        )
    return table


def _entries_for(db: Session, dimension: str, list_type: str, now) -> dict[str, ListHit]:
    """取某一维度的名单表：优先 Redis 缓存，失败则直查数据库。

    缓存结构用 **HASH**（field=value，value=JSON 序列化的命中详情），
    而不是"SET 存值 + 命中后回查库"：
        - HASH 一次 HGETALL 就能拿到命中详情，无需二次查库；
        - 空 HASH 能被 EXISTS 区分"已缓存且为空"与"尚未缓存"，
          后者才需要回源 —— 这样也顺带挡住了"名单表为空时每次决策都查库"的穿透。
    缓存存的是**命中详情**而非"值是否存在"，因为决策落库需要
    priority / reason / entry_id 这些信息。
    """
    redis = get_redis()
    key = list_key(dimension, list_type)
    try:
        if redis.exists(key):
            raw = redis.hgetall(key)
            if not raw:
                return {}
            return {
                value: ListHit(**orjson.loads(payload))
                for value, payload in raw.items()
                if value != EMPTY_SENTINEL
            }
        table = _load_from_db(db, dimension, list_type, now)
        pipe = redis.pipeline(transaction=False)
        pipe.delete(key)
        if table:
            pipe.hset(
                key,
                mapping={
                    value: orjson.dumps(hit.to_dict()).decode("utf-8")
                    for value, hit in table.items()
                },
            )
        else:
            # 空表也落一个占位，让下次 exists 为真，避免穿透
            pipe.hset(key, EMPTY_SENTINEL, "1")
        pipe.expire(key, LIST_TTL)
        pipe.execute()
        return table
    except (RedisError, orjson.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("名单缓存不可用，降级为直查数据库：%s", exc)
        return _load_from_db(db, dimension, list_type, now)


def _pick(hits: list[ListHit], policy: str, black: list[ListHit], white: list[ListHit]) -> str | None:
    """黑白同时命中时的仲裁。

    ``priority`` 越小优先级越高（与 rc_list_entry 的字段语义一致）。
    """
    if policy == "whitelist_first":
        return "Pass"
    if policy == "priority":
        best_black = min(black, key=lambda item: item.priority)
        best_white = min(white, key=lambda item: item.priority)
        return "Reject" if best_black.priority <= best_white.priority else "Pass"
    # 默认 black_first：风控场景宁可误拦（可申诉）也不误放
    return "Reject"


def match(
    db: Session,
    *,
    subject: dict[str, Any],
    now=None,
) -> ListMatchResult:
    """对一次事件的主体做五维度名单匹配。

    ``subject`` 使用事件信封的字段名（``user_id`` / ``phone`` / ``ip`` /
    ``device_id`` / ``address_hash``），由调用方从事件里取。
    """
    now = now or utcnow()
    result = ListMatchResult()
    try:
        result.policy = get_str(db, "list_conflict_policy")
    except Exception:  # noqa: BLE001 - 配置不可用时按默认策略走，不影响主流程
        result.policy = "black_first"

    black_hits: list[ListHit] = []
    white_hits: list[ListHit] = []
    gray_hits: list[ListHit] = []

    for dimension in ALL_DIMENSIONS:
        value = subject.get(DIMENSION_FIELD[dimension])
        if not value:
            # 主体维度缺失（如事件无地址）：该维度无法匹配，跳过而非报错
            continue
        value = str(value)

        for list_type, bucket in (
            (LIST_BLACK, black_hits),
            (LIST_WHITE, white_hits),
            (LIST_GRAY, gray_hits),
        ):
            table = _entries_for(db, dimension, list_type, now)
            hit = table.get(value)
            if hit is not None:
                bucket.append(hit)

    result.hits = black_hits + white_hits + gray_hits

    if black_hits and white_hits:
        result.conflict = True
        decision = _pick(result.hits, result.policy, black_hits, white_hits)
    elif black_hits:
        decision = "Reject"
    elif white_hits:
        decision = "Pass"
    else:
        decision = None

    result.decision = decision
    result.decided_by_list = decision is not None

    # 先铺满默认值（未命中 = 0/False），再覆盖命中项：
    # 规则因此总是拿到确定的布尔/数值，不会因为"这次没命中任何名单"
    # 而把 subject_gray_flag 变成缺失字段。
    for flag_key in GRAY_FLAG_KEYS.values():
        result.flags[flag_key] = 0
    result.flags[BLACKLIST_FLAG_KEY] = False
    result.flags[WHITELIST_FLAG_KEY] = False

    # 灰名单转为加成特征（不决定动作，只提高命中概率）
    for hit in gray_hits:
        flag_key = GRAY_FLAG_KEYS.get(hit.dimension, "subject_gray_flag")
        result.flags[flag_key] = 1
    if black_hits:
        result.flags[BLACKLIST_FLAG_KEY] = True
    if white_hits:
        result.flags[WHITELIST_FLAG_KEY] = True

    return result


def invalidate_cache(*, dimension: str | None = None, list_type: str | None = None) -> int:
    """写名单后主动失效缓存，返回删除的键数。

    只删受影响的维度（而不是 FLUSHDB）：名单写操作是低频动作，
    但全局清缓存会在写名单的瞬间让所有决策退化到查库，没必要。
    """
    redis = get_redis()
    keywords = list_key(dimension or "*", list_type or "*")
    pattern = keywords.replace(":*", ":*")
    removed = 0
    try:
        for key in redis.scan_iter(match=pattern, count=100):
            redis.delete(key)
            removed += 1
    except RedisError as exc:
        logger.warning("名单缓存失效失败（不影响正确性，TTL 60s 后自然过期）：%s", exc)
    return removed
