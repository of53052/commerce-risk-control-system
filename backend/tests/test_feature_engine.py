"""特征引擎测试：滑动窗口计数/求和/去重、衍生比例、缺失登记。

需要真实 Redis（DB=1）与测试库；这就是它被标 integration 的原因。
重点覆盖的风险：
    1. 窗口边界用**事件自身时间戳**——用服务器时间会让历史重放结果与实时不一致；
    2. 聚簇去重靠条带 member 的 ``{prefix}:{subject}`` 约定，写读两侧必须一致；
    3. 当前事件自身尚未入条带，计数/求和/去重都要把它补上（否则首单永远算不到自己）；
    4. 实体缺失时返回 0 并登记 missing_fields，而不是抛错打挂决策链；
    5. ratio 特征的分子可能带窗口也可能不带（device_new_account_cnt 是画像特征）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.timeutil import to_ms, utcnow
from app.models.biz import BizCustomer, BizOrder
from app.models.event import (
    EVENT_AFTER_SALE_APPLY,
    EVENT_COUPON_RECEIVE,
    EVENT_LOGIN,
    EVENT_ORDER_CREATE,
)
from app.services import feature_engine, window_store
from app.services.window_store import ENTITY_DEVICE, ENTITY_IP, ENTITY_USER

pytestmark = pytest.mark.integration

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 604800}


def _seed_event(
    *,
    user_id: str,
    event_type: str,
    occurred_at,
    event_id: str,
    amount: float | None = None,
    device_id: str | None = None,
    ip: str | None = None,
    windows=WINDOWS,
    redis_client=None,
) -> None:
    """把一个事件写进条带（模拟 event_gateway 的写入动作）。

    聚簇实体（设备/IP）的 member 需要写成 ``user:{user_id}``，
    与 feature_engine 的去重读取约定一致。
    """
    ts_ms = to_ms(occurred_at)
    entities = [(ENTITY_USER, user_id)]
    if device_id:
        entities.append((ENTITY_DEVICE, device_id))
    if ip:
        entities.append((ENTITY_IP, ip))

    window_store.add_event_to_entities(
        entities=entities,
        event_type=event_type,
        windows=windows,
        ts_ms=ts_ms,
        event_id=event_id,
        amount=amount,
        client=redis_client,
    )
    # 聚簇条带单独写：member 用 user:{user_id} 承载主体标识
    for entity, entity_id in ((ENTITY_DEVICE, device_id), (ENTITY_IP, ip)):
        if not entity_id:
            continue
        for window, seconds in windows.items():
            window_store.add_event(
                entity=entity,
                entity_id=entity_id,
                event_type=event_type,
                window=window,
                window_seconds=seconds,
                ts_ms=ts_ms,
                event_id=f"user:{user_id}",
                amount=amount,
                client=redis_client,
            )


def test_user_coupon_cnt_across_windows(db, redis_client) -> None:
    """1h / 24h / 7d 三档窗口应各算到落在窗口内的条数。"""
    now = utcnow()
    for index, delta in enumerate([timedelta(minutes=10), timedelta(hours=3), timedelta(days=3)]):
        _seed_event(
            user_id="U1",
            event_type=EVENT_COUPON_RECEIVE,
            occurred_at=now - delta,
            event_id=f"E{index}",
            amount=20,
            redis_client=redis_client,
        )

    result = feature_engine.compute(
        db,
        event_type=EVENT_COUPON_RECEIVE,
        user_id="U1",
        payload={"face_value": 5},
        occurred_at=now,
    )

    assert result.get("user_coupon_cnt_1h") == 1
    assert result.get("user_coupon_cnt_24h") == 2
    assert result.get("user_coupon_cnt_7d") == 3


def test_sum_includes_current_event(db, redis_client) -> None:
    """金额求和要把"当前这条尚未入条带的事件"算进去。"""
    now = utcnow()
    _seed_event(
        user_id="U2",
        event_type=EVENT_COUPON_RECEIVE,
        occurred_at=now - timedelta(minutes=5),
        event_id="E1",
        amount=30,
        redis_client=redis_client,
    )

    result = feature_engine.compute(
        db,
        event_type=EVENT_COUPON_RECEIVE,
        user_id="U2",
        payload={"face_value": 20},
        occurred_at=now,
    )
    assert result.get("user_coupon_amount_24h") == 50.0


def test_window_boundary_excludes_old_events(db, redis_client) -> None:
    """超出窗口的事件不能被计入（边界用事件时间戳）。"""
    now = utcnow()
    _seed_event(
        user_id="U3",
        event_type=EVENT_COUPON_RECEIVE,
        occurred_at=now - timedelta(hours=2),
        event_id="OLD",
        redis_client=redis_client,
    )
    result = feature_engine.compute(
        db, event_type=EVENT_COUPON_RECEIVE, user_id="U3", occurred_at=now
    )
    assert result.get("user_coupon_cnt_1h") == 0
    assert result.get("user_coupon_cnt_24h") == 1


def test_replay_is_deterministic(db, redis_client) -> None:
    """用历史时间戳重放：同一时间点的两次计算必须一致（可复现性）。"""
    now = utcnow()
    _seed_event(
        user_id="U4",
        event_type=EVENT_ORDER_CREATE,
        occurred_at=now - timedelta(minutes=30),
        event_id="E1",
        amount=99.5,
        redis_client=redis_client,
    )
    first = feature_engine.compute(db, event_type=EVENT_ORDER_CREATE, user_id="U4", occurred_at=now)
    second = feature_engine.compute(db, event_type=EVENT_ORDER_CREATE, user_id="U4", occurred_at=now)
    assert first.features["user_order_cnt_24h"] == second.features["user_order_cnt_24h"] == 1
    assert first.features["user_order_amount_24h"] == second.features["user_order_amount_24h"]


def test_device_account_cnt_counts_distinct_users(db, redis_client) -> None:
    """同设备关联账号数：同一账号多次登录只算一个。"""
    now = utcnow()
    for index, user in enumerate(["U5", "U5", "U6"]):
        _seed_event(
            user_id=user,
            event_type=EVENT_LOGIN,
            occurred_at=now - timedelta(minutes=10 * (index + 1)),
            event_id=f"E{index}",
            device_id="D1",
            redis_client=redis_client,
        )

    result = feature_engine.compute(
        db, event_type=EVENT_LOGIN, user_id="U7", device_id="D1", occurred_at=now
    )
    # 历史 2 个账号（U5/U6）+ 当前事件主体 U7 = 3
    assert result.get("device_account_cnt_24h") == 3


def test_missing_entity_is_zero_and_recorded(db, redis_client) -> None:
    """事件没带地址：地址类特征记 0 并进入 missing_fields。"""
    result = feature_engine.compute(db, event_type=EVENT_ORDER_CREATE, user_id="U8", occurred_at=utcnow())
    assert result.get("address_account_cnt_7d") == 0
    assert "address_account_cnt_7d" in result.missing_fields
    assert "address_refund_cnt_7d" in result.missing_fields


def test_account_age_days_from_business_table(db, redis_client) -> None:
    """账号注册天数来自业务表，而不是 Redis。"""
    now = utcnow()
    db.add(BizCustomer(user_id="U9", phone="13800000009", register_at=now - timedelta(days=12)))
    db.flush()

    result = feature_engine.compute(db, event_type=EVENT_LOGIN, user_id="U9", occurred_at=now)
    assert result.get("account_age_days") == 12


def test_account_age_days_missing_for_unknown_user(db, redis_client) -> None:
    result = feature_engine.compute(db, event_type=EVENT_LOGIN, user_id="NOPE", occurred_at=utcnow())
    assert result.get("account_age_days") is None
    assert "account_age_days" in result.missing_fields


def test_refund_rate_ratio(db, redis_client) -> None:
    """退款率 = 退款申请数 / 订单数；分母为 0 时取 0。"""
    now = utcnow()
    for index in range(4):
        _seed_event(
            user_id="U10",
            event_type=EVENT_ORDER_CREATE,
            occurred_at=now - timedelta(minutes=5 * (index + 1)),
            event_id=f"O{index}",
            redis_client=redis_client,
        )
    _seed_event(
        user_id="U10",
        event_type=EVENT_AFTER_SALE_APPLY,
        occurred_at=now - timedelta(minutes=1),
        event_id="R0",
        redis_client=redis_client,
    )

    result = feature_engine.compute(db, event_type=EVENT_AFTER_SALE_APPLY, user_id="U10", occurred_at=now)
    assert result.get("user_refund_cnt_24h") == 1
    assert result.get("user_order_cnt_24h") == 4
    assert result.get("user_refund_rate_24h") == pytest.approx(0.25)


def test_refund_rate_zero_when_no_orders(db, redis_client) -> None:
    result = feature_engine.compute(db, event_type=EVENT_AFTER_SALE_APPLY, user_id="U11", occurred_at=utcnow())
    assert result.get("user_refund_rate_24h") == 0.0


def test_device_new_account_ratio_uses_windowless_numerator(db, redis_client) -> None:
    """device_new_account_cnt 是无窗口画像特征，ratio 查值必须能回退到基名。"""
    now = utcnow()
    db.add(BizCustomer(user_id="U12", phone="13800000012", register_at=now - timedelta(days=2)))
    db.flush()
    _seed_event(
        user_id="U12",
        event_type=EVENT_LOGIN,
        occurred_at=now - timedelta(minutes=20),
        event_id="E1",
        device_id="D2",
        redis_client=redis_client,
    )

    result = feature_engine.compute(
        db, event_type=EVENT_LOGIN, user_id="U12", device_id="D2", occurred_at=now
    )
    assert result.get("device_new_account_cnt") == 1
    assert result.get("device_account_cnt_24h") == 1
    assert result.get("device_new_account_ratio_24h") == pytest.approx(1.0)


def test_env_risk_score_weights(db, redis_client) -> None:
    """环境指纹风险分：可疑信号按固定权重累加，上限 100。"""
    result = feature_engine.compute(
        db,
        event_type=EVENT_LOGIN,
        user_id="U13",
        payload={"fingerprint": {"is_emulator": True, "is_rooted": True, "is_multi_app": True}},
        occurred_at=utcnow(),
    )
    assert result.get("device_env_risk") == 85


def test_flag_features_default_false(db, redis_client) -> None:
    """标记类特征必须有确定默认值，不能让规则拿到"这个键不存在"。"""
    result = feature_engine.compute(db, event_type=EVENT_LOGIN, user_id="U14", occurred_at=utcnow())
    assert result.get("subject_blacklist") is False
    assert result.get("user_first_order_refund") is False


def test_first_order_refund_flag(db, redis_client) -> None:
    now = utcnow()
    db.add(BizCustomer(user_id="U15", phone="13800000015", register_at=now - timedelta(days=5)))
    db.add(
        BizOrder(
            order_no="ORD15",
            user_id="U15",
            product_id="P1",
            quantity=1,
            amount=199,
            status="paid",
        )
    )
    db.flush()

    result = feature_engine.compute(db, event_type=EVENT_AFTER_SALE_APPLY, user_id="U15", occurred_at=now)
    assert result.get("user_first_order_refund") is True


def test_list_flags_override_defaults(db, redis_client) -> None:
    """名单服务给出的标记要覆盖默认值。"""
    result = feature_engine.compute(
        db,
        event_type=EVENT_LOGIN,
        user_id="U16",
        occurred_at=utcnow(),
        list_flags={"subject_blacklist": True, "subject_gray_flag": 1},
    )
    assert result.get("subject_blacklist") is True
    assert result.get("subject_gray_flag") == 1


def test_registry_covers_prd_feature_keys() -> None:
    """注册表必须覆盖 PRD 7.2 列出的特征（防止改名/漏登记）。"""
    keys = set(feature_engine.registry_keys())
    required = {
        "user_login_cnt_1h", "user_coupon_cnt_1h", "user_coupon_amount_24h",
        "user_order_cnt_1h", "user_order_amount_24h", "user_refund_cnt_24h",
        "user_refund_rate_24h", "user_refund_amount_24h", "user_first_order_refund",
        "device_account_cnt_24h", "device_coupon_cnt_1h", "device_order_cnt_1h",
        "device_new_account_ratio_24h", "device_env_risk",
        "ip_account_cnt_24h", "ip_coupon_cnt_1h", "ip_order_cnt_1h",
        "ip_region_mismatch", "ip_is_datacenter",
        "address_account_cnt_7d", "address_refund_cnt_7d", "address_refund_rate_7d",
        "address_phone_share_cnt",
        "account_age_days", "subject_case_cnt", "subject_blacklist", "night_activity_ratio",
    }
    assert required <= keys, f"注册表缺少：{sorted(required - keys)}"

