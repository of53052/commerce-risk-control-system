"""名单服务测试：五维度匹配、黑白冲突仲裁、灰名单加成、缓存与失效。

重点覆盖的风险：
    1. 黑白同时命中若没有明确的仲裁规则，行为会随代码巧合变化 ——
       默认必须"黑优先"（宁可误拦可申诉，不可误放）；
    2. 过期名单若物理删除，就无法作为审计证据；必须逻辑失效；
    3. 名单缓存失效若不彻底，会出现"刚拉黑却还在放行"的事故，
       因此写入后必须能主动清缓存；
    4. 维度缺失（事件无地址）不能报错，只跳过该维度。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.timeutil import utcnow
from app.models.rclist import (
    LIST_BLACK,
    LIST_GRAY,
    LIST_WHITE,
    SOURCE_MANUAL,
    STATUS_ACTIVE,
    STATUS_INACTIVE,
    RcListEntry,
)
from app.services import list_service

pytestmark = pytest.mark.integration


def _entry(
    *,
    list_type: str,
    dimension: str,
    value: str,
    priority: int = 100,
    reason: str | None = None,
    expire_at=None,
    status: str = STATUS_ACTIVE,
) -> RcListEntry:
    return RcListEntry(
        list_type=list_type,
        dimension=dimension,
        value=value,
        priority=priority,
        reason=reason,
        source=SOURCE_MANUAL,
        expire_at=expire_at,
        status=status,
        created_by="test",
    )


def test_blacklist_hit_rejects(db, redis_client) -> None:
    db.add(_entry(list_type=LIST_BLACK, dimension="user", value="U1", reason="批量注册"))
    db.flush()

    result = list_service.match(db, subject={"user_id": "U1"})
    assert result.decision == "Reject"
    assert result.decided_by_list is True
    assert result.flags["subject_blacklist"] is True
    assert result.hits[0].reason == "批量注册"


def test_whitelist_hit_passes(db, redis_client) -> None:
    db.add(_entry(list_type=LIST_WHITE, dimension="user", value="VIP1"))
    db.flush()

    result = list_service.match(db, subject={"user_id": "VIP1"})
    assert result.decision == "Pass"
    assert result.flags["subject_whitelist"] is True


def test_no_hit_returns_none(db, redis_client) -> None:
    result = list_service.match(db, subject={"user_id": "NOBODY"})
    assert result.decision is None
    assert result.decided_by_list is False
    assert result.hits == []


def test_black_wins_by_default(db, redis_client) -> None:
    """默认 black_first：黑白同时命中时判 Reject 并标记冲突。"""
    db.add(_entry(list_type=LIST_BLACK, dimension="user", value="U2"))
    db.add(_entry(list_type=LIST_WHITE, dimension="device", value="D2"))
    db.flush()

    result = list_service.match(db, subject={"user_id": "U2", "device_id": "D2"})
    assert result.decision == "Reject"
    assert result.conflict is True
    assert result.policy == "black_first"


@pytest.mark.parametrize(
    ("policy", "priority_black", "priority_white", "expected"),
    [
        ("whitelist_first", 10, 200, "Pass"),
        ("priority", 10, 200, "Reject"),   # 黑优先级更高（数值小）
        ("priority", 200, 10, "Pass"),     # 白优先级更高
        ("priority", 50, 50, "Reject"),    # 平手按黑优先
    ],
)
def test_conflict_policy_configurable(
    db, redis_client, policy: str, priority_black: int, priority_white: int, expected: str
) -> None:
    from app.models.sys import SysConfig
    from app.services.config_service import invalidate_cache

    db.add(
        SysConfig(
            config_key="list_conflict_policy",
            config_value=policy,
            value_type="str",
            description="测试覆盖",
            updated_by="test",
        )
    )
    db.add(_entry(list_type=LIST_BLACK, dimension="user", value="U3", priority=priority_black))
    db.add(_entry(list_type=LIST_WHITE, dimension="user", value="U3", priority=priority_white))
    db.flush()
    invalidate_cache()

    result = list_service.match(db, subject={"user_id": "U3"})
    assert result.decision == expected
    assert result.policy == policy


def test_graylist_becomes_feature_flag_only(db, redis_client) -> None:
    """灰名单不决定动作，只转成加成特征。

    注意：flags 里始终包含全部标记键（未命中为 0/False），
    这样规则引用 subject_gray_flag 之类字段时永远拿到确定值，不会变成"缺失字段"。
    """
    db.add(_entry(list_type=LIST_GRAY, dimension="ip", value="10.0.0.9"))
    db.flush()

    result = list_service.match(db, subject={"ip": "10.0.0.9"})
    assert result.decision is None
    assert result.decided_by_list is False
    assert result.flags["ip_gray_flag"] == 1
    assert result.flags["device_gray_flag"] == 0
    assert result.flags["subject_blacklist"] is False
    assert set(result.flags) == {
        "subject_gray_flag", "phone_gray_flag", "ip_gray_flag",
        "device_gray_flag", "address_gray_flag",
        "subject_blacklist", "subject_whitelist",
    }


def test_expired_entry_is_ignored_but_kept(db, redis_client) -> None:
    """过期名单不命中，但记录仍留在库里（审计证据）。"""
    db.add(
        _entry(
            list_type=LIST_BLACK,
            dimension="user",
            value="U4",
            expire_at=utcnow() - timedelta(hours=1),
        )
    )
    db.flush()

    result = list_service.match(db, subject={"user_id": "U4"})
    assert result.decided_by_list is False
    assert db.query(RcListEntry).filter_by(value="U4").count() == 1


def test_inactive_entry_is_ignored(db, redis_client) -> None:
    db.add(_entry(list_type=LIST_BLACK, dimension="user", value="U5", status=STATUS_INACTIVE))
    db.flush()
    assert list_service.match(db, subject={"user_id": "U5"}).decided_by_list is False


def test_multiple_dimensions_all_matched(db, redis_client) -> None:
    """五个维度都要参与匹配。"""
    db.add(_entry(list_type=LIST_BLACK, dimension="ip", value="1.1.1.1"))
    db.add(_entry(list_type=LIST_BLACK, dimension="device", value="D9"))
    db.add(_entry(list_type=LIST_BLACK, dimension="phone", value="13900000009"))
    db.add(_entry(list_type=LIST_BLACK, dimension="address", value="ADDR9"))
    db.flush()

    result = list_service.match(
        db,
        subject={
            "user_id": "U9",
            "phone": "13900000009",
            "ip": "1.1.1.1",
            "device_id": "D9",
            "address_hash": "ADDR9",
        },
    )
    assert len(result.hits) == 4
    assert {(hit.dimension) for hit in result.hits} == {"ip", "device", "phone", "address"}


def test_missing_subject_dimension_is_skipped(db, redis_client) -> None:
    """事件没有地址等维度时不能报错。"""
    db.add(_entry(list_type=LIST_BLACK, dimension="address", value="ADDRX"))
    db.flush()
    result = list_service.match(db, subject={"user_id": "U10"})
    assert result.decided_by_list is False


def test_cache_hit_after_first_match(db, redis_client) -> None:
    """第二次匹配应命中缓存（同名 HASH 键存在），且结果一致。"""
    from app.db.redis_client import list_key

    db.add(_entry(list_type=LIST_BLACK, dimension="user", value="U11"))
    db.flush()

    first = list_service.match(db, subject={"user_id": "U11"})
    assert redis_client.exists(list_key("user", LIST_BLACK)) == 1
    second = list_service.match(db, subject={"user_id": "U11"})
    assert first.decision == second.decision == "Reject"


def test_cache_invalidation_after_write(db, redis_client) -> None:
    """写入名单后主动失效缓存，使新名单立即生效。"""
    from app.db.redis_client import list_key

    # 先建立一次空缓存
    assert list_service.match(db, subject={"user_id": "U12"}).decided_by_list is False
    assert redis_client.exists(list_key("user", LIST_BLACK)) == 1

    db.add(_entry(list_type=LIST_BLACK, dimension="user", value="U12"))
    db.flush()
    removed = list_service.invalidate_cache(dimension="user", list_type=LIST_BLACK)
    assert removed >= 1
    assert list_service.match(db, subject={"user_id": "U12"}).decision == "Reject"


def test_empty_list_is_cached_to_avoid_penetration(db, redis_client) -> None:
    """名单表为空时也要落缓存占位，避免每次决策都查库。"""
    from app.db.redis_client import list_key

    list_service.match(db, subject={"user_id": "U13"})
    assert redis_client.exists(list_key("user", LIST_BLACK)) == 1
