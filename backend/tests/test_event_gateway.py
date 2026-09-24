"""事件网关与接入接口的集成测试（P0 验收 #1 / #2 / #3 的主证据）。

覆盖的风险按重要性排序：

1. **三条决策路径**（Pass / Review / Reject）必须都能走通并落库 ——
   这是 PRD §17.2 验收标准 3 的原文要求；
2. **幂等**：同一 event_id 重复投递只能产生一条 rc_event 与一条 rc_decision，
   且第二次必须回放首次结果（业务方重试是常态，重复决策等于重复拦截）；
3. **五类事件**都能被解析、特征计算不报错、决策落库；
4. **条带写入与特征读取口径一致**：写错 member 格式不会报错，
   只会让聚集度特征恒为 0 —— 这类"静默失效"必须由测试挡住；
5. **批量接口的部分失败**：一条坏数据不能连累整批。

隔离方式见 tests/conftest.py：本文件使用 ``gw_db`` 夹具（真实提交 + 每例清表），
因为网关的语义就是"提交后再返回"，用回滚夹具测不出真实行为。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import hash_api_key, hash_password
from app.core.timeutil import utcnow
from app.db.session import get_db
from app.expression import node_to_dict, parse
from app.models.biz import BizCustomer, BizOrder
from app.models.case import RcCase
from app.models.decision import RcDecision, RcDecisionHit, RcModelContribution
from app.models.event import RcEvent, RcFeatureSnapshot
from app.models.rclist import DIM_USER, LIST_BLACK, LIST_WHITE, STATUS_ACTIVE, RcListEntry
from app.models.rule import ACTION_HINT_CHALLENGE, CATEGORY_FREQUENCY, RcRule
from app.models.sys import ROLE_ADMIN, STATUS_ENABLED, SysApiKey, SysUser
from app.schemas.event import EventIn
from app.services import config_service, event_gateway, list_service, model_engine, rule_engine

pytestmark = pytest.mark.integration

# 每个测试前清空的表：既包含决策产物，也包含策略与业务单据 ——
# 让每个测试从"空策略 + 默认配置"起步，结论只依赖该测试自己造的数据。
_TRUNCATE_TABLES = (
    # 案件四表：P1 起 Review/Reject 会真实建案，不清空则上一个用例的案件
    # 会被下一个用例合案（表现为 event_cnt 比预期大），断言随之飘。
    "rc_case",
    "rc_case_event",
    "rc_case_action",
    "rc_case_action_item",
    "rc_event",
    "rc_feature_snapshot",
    "rc_decision",
    "rc_decision_hit",
    "rc_model_contribution",
    "rc_audit_log",
    "rc_rule",
    "rc_list_entry",
    "sys_config",
    "sys_api_key",
    "sys_user",
    "biz_order",
    "biz_refund",
    "biz_customer",
    "biz_coupon_receive",
)


def _truncate(db: Session) -> None:
    """清空业务表。

    用 TRUNCATE 而不是 DELETE：rc_audit_log 上有"只增"触发器（BEFORE DELETE 直接
    抛错），DELETE 会被挡住；TRUNCATE 是 DDL，不触发 DML 触发器。
    执行前必须确保当前会话没有未提交事务 —— 否则 DDL 与未提交的 DML 争抢
    表级元数据锁会**静默挂死**（无异常、无输出，只能靠 pytest-timeout 兜住）。
    """
    db.rollback()
    for name in _TRUNCATE_TABLES:
        db.execute(text(f"TRUNCATE TABLE {name}"))
    db.commit()


@pytest.fixture
def gw_db(engine, redis_client) -> Session:
    """会真正提交的会话（网关内部自己 commit）。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    _truncate(session)
    config_service.invalidate_cache()
    rule_engine.invalidate_cache()
    list_service.invalidate_cache()
    model_engine.reset()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(gw_db) -> TestClient:
    """把应用的 get_db 依赖换成测试会话，保证接口与测试看到同一份数据。"""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: gw_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# 测试数据构造
# --------------------------------------------------------------------------- #
def make_event(
    *,
    event_type: str = "login",
    event_id: str | None = None,
    user_id: str = "U1",
    phone: str = "13800000001",
    device_id: str = "DEV-1",
    ip: str = "10.1.1.1",
    occurred_at=None,
    payload: dict | None = None,
    fingerprint: dict | None = None,
    is_proxy: bool = False,
    address_hash: str | None = None,
    source: str = "simulation",
) -> EventIn:
    """构造一条合法事件（字段含义见 docs/PRD.md §6.1）。"""
    default_payloads = {
        "login": {"login_type": "password", "login_result": "success"},
        "coupon_receive": {"coupon_id": "CP-1", "coupon_name": "满减券", "face_value": 50, "channel": "app"},
        "order_create": {"order_no": "SO-1", "product_id": "P-1", "quantity": 1, "amount": 199, "pay_method": "balance"},
        "order_pay": {"order_no": "SO-1", "pay_amount": 199, "pay_channel": "balance", "pay_status": "success"},
        "after_sale_apply": {
            "refund_no": "RF-1",
            "order_no": "SO-1",
            "refund_amount": 199,
            "reason": "未收到货",
            "apply_type": "refund_only",
        },
    }
    data = {
        "event_id": event_id or f"EVT-{event_type}-{utcnow().strftime('%H%M%S%f')}",
        "event_type": event_type,
        "occurred_at": occurred_at or utcnow(),
        "user_id": user_id,
        "phone": phone,
        "device": {"device_id": device_id, "fingerprint": fingerprint or {"os": "Android", "screen": "1080x2340"}},
        "network": {"ip": ip, "ip_region": "CN-SH", "is_proxy": is_proxy},
        "payload": payload if payload is not None else default_payloads[event_type],
        "source": source,
    }
    if address_hash:
        data["address"] = {"address_hash": address_hash, "region": "CN-SH", "phone": phone}
    return EventIn(**data)


def add_rule(
    db: Session,
    *,
    code: str,
    text: str,
    score: int,
    scene: str = "all",
    action_hint: str = "none",
    enabled: bool = True,
) -> RcRule:
    """插入一条规则并让编译缓存失效（否则改动不生效，测试会莫名用旧规则）。"""
    rule = RcRule(
        code=code,
        name=f"测试规则 {code}",
        scene=scene,
        category=CATEGORY_FREQUENCY,
        condition=node_to_dict(parse(text)),
        condition_text=text,
        score=score,
        action_hint=action_hint,
        priority=100,
        enabled=enabled,
        version=1,
        created_by="test",
        updated_by="test",
    )
    db.add(rule)
    db.commit()
    rule_engine.invalidate_cache()
    return rule


def count_rows(db: Session, model) -> int:
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


# --------------------------------------------------------------------------- #
# 决策主链路
# --------------------------------------------------------------------------- #
def test_login_event_pass_path(gw_db: Session) -> None:
    """无规则命中 + 无模型 -> 综合分 0 -> Pass，且五张表都落到了数据。"""
    result = event_gateway.handle_event(gw_db, event=make_event(event_type="login"))

    assert result.response["action"] == "Pass"
    assert result.response["risk_level"] == "low"
    assert result.response["decided_by"] == "rule_model"
    assert result.response["rule_score"] == 0
    assert result.response["model_score"] == 0
    assert result.duplicated is False

    assert count_rows(gw_db, RcEvent) == 1
    assert count_rows(gw_db, RcDecision) == 1
    assert count_rows(gw_db, RcFeatureSnapshot) == 1
    # 无启用模型：不得写入"模型未参与"的贡献行（否则 top-5 会全是伪造的 0 贡献）
    assert count_rows(gw_db, RcModelContribution) == 0

    snapshot = gw_db.execute(select(RcFeatureSnapshot)).scalar_one()
    # 当前事件本身必须计入窗口特征（事件在决策后才入条带，见 feature_engine._compute_sum）
    assert snapshot.features["user_login_cnt_1h"] == 1
    # 快照只存注册表里的特征，不塞规则用的原始上下文（device_fingerprint 等）
    assert "fingerprint" not in snapshot.features
    assert "device_fingerprint" not in snapshot.features
    # 但响应要在 context 里带上它们，否则审核员无法核对证据
    assert result.response["context"]["event"]["device_fingerprint"]["os"] == "Android"
    # 手机号必须脱敏后才进响应（落库仍为明文，由数据库权限保护）
    assert result.response["context"]["event"]["phone"] == "138****0001"


def test_duplicate_event_replays_first_decision(gw_db: Session) -> None:
    """幂等：第二次投递必须回放首次结果，且不产生第二条决策。"""
    event = make_event(event_type="login", event_id="EVT-IDEM-0001")
    first = event_gateway.handle_event(gw_db, event=event)
    second = event_gateway.handle_event(gw_db, event=event)

    assert first.duplicated is False
    assert second.duplicated is True
    assert second.decision_id == first.decision_id
    assert second.response["decision_id"] == first.response["decision_id"]
    assert second.response["from_cache"] is True
    assert count_rows(gw_db, RcEvent) == 1
    assert count_rows(gw_db, RcDecision) == 1


def test_duplicate_event_falls_back_to_db_when_cache_cleared(gw_db: Session, redis_client) -> None:
    """Redis 幂等缓存被清空后，重复投递仍要回放首次决策（回源数据库还原）。"""
    event = make_event(event_type="login", event_id="EVT-IDEM-0002")
    first = event_gateway.handle_event(gw_db, event=event)
    redis_client.flushdb()

    second = event_gateway.handle_event(gw_db, event=event)
    assert second.duplicated is True
    assert second.response["decision_id"] == first.response["decision_id"]
    assert second.response["action"] == first.response["action"]
    assert count_rows(gw_db, RcEvent) == 1
    assert count_rows(gw_db, RcDecision) == 1
    # 还原路径必须如实说明"这份响应来自数据库"，不能假装是缓存命中
    assert any("还原" in note for note in second.response["notes"])


def test_review_path_when_score_reaches_review_threshold(gw_db: Session) -> None:
    """规则分达到审核阈值（默认 60）-> Review，P1 起真实建案。"""
    add_rule(gw_db, code="RC-T-REVIEW", text="user_login_cnt_24h >= 1", score=70)
    result = event_gateway.handle_event(gw_db, event=make_event(event_type="login"))

    assert result.response["action"] == "Review"
    assert result.response["risk_level"] == "mid"
    assert result.response["risk_score"] == 70
    assert result.response["case_no"] is not None
    assert result.response["case_no"].startswith("C")
    assert any("已生成案件" in note for note in result.response["notes"])
    assert count_rows(gw_db, RcDecisionHit) == 1
    hit = gw_db.execute(select(RcDecisionHit)).scalar_one()
    assert hit.rule_code == "RC-T-REVIEW"
    assert hit.score == 70
    # 证据链必须落库：审核员点开案件要能看到"为什么命中"
    assert hit.evidence
    assert hit.evidence[0]["field"] == "user_login_cnt_24h"


def test_reject_path_at_high_score(gw_db: Session) -> None:
    """综合分达到拒绝阈值（默认 80）-> Reject / 高风险。"""
    add_rule(gw_db, code="RC-T-REJECT", text="user_login_cnt_24h >= 1", score=85)
    rejected = event_gateway.handle_event(gw_db, event=make_event(event_type="login"))
    assert rejected.response["action"] == "Reject"
    assert rejected.response["risk_level"] == "high"
    assert rejected.response["risk_score"] == 85


def test_challenge_only_when_rule_declares_it(gw_db: Session) -> None:
    """60~79 区间：规则声明 challenge 才走 Challenge，且 Challenge 不建案。"""
    add_rule(
        gw_db,
        code="RC-T-CHALLENGE",
        text="user_login_cnt_24h >= 1",
        score=65,
        action_hint=ACTION_HINT_CHALLENGE,
    )
    challenged = event_gateway.handle_event(gw_db, event=make_event(event_type="login"))
    assert challenged.response["action"] == "Challenge"
    assert challenged.response["action_hint"] == "challenge"
    assert challenged.response["risk_level"] == "mid"
    assert challenged.response["case_no"] is None
    assert not any("已生成案件" in note or "合并到案件" in note for note in challenged.response["notes"])


def test_review_is_default_in_mid_band(gw_db: Session) -> None:
    """60~79 且规则未声明 challenge -> Review（默认走人工审核而不是二次验证）。"""
    add_rule(gw_db, code="RC-T-MID", text="user_login_cnt_24h >= 1", score=65)
    result = event_gateway.handle_event(gw_db, event=make_event(event_type="login"))
    assert result.response["action"] == "Review"
    assert result.response["action_hint"] == "none"


def test_reject_creates_case_and_merges_within_window(gw_db: Session) -> None:
    """P1 闭环：Reject 建案并把 case_no 写回决策与响应；窗口内第二次触发合并。

    这条测试是"决策 → 案件"的接缝证据：链路两侧各自的单测都通过，
    但接缝没接上（case_no 恒为 None）时只有它会把问题暴露出来。
    """
    add_rule(gw_db, code="RC-T-CASE", text="user_login_cnt_24h >= 1", score=85)

    first = event_gateway.handle_event(
        gw_db, event=make_event(event_type="login", event_id="EVT-CASE-1", user_id="U9")
    )
    assert first.response["action"] == "Reject"
    assert first.case_no is not None
    assert first.response["case_no"] == first.case_no

    decision = gw_db.execute(select(RcDecision)).scalars().all()
    assert len(decision) == 1
    assert decision[0].case_no == first.case_no

    second = event_gateway.handle_event(
        gw_db,
        event=make_event(
            event_type="login",
            event_id="EVT-CASE-2",
            user_id="U9",
            occurred_at=utcnow() + timedelta(minutes=1),
        ),
    )
    # 同主体同场景在合案窗口内 -> 复用同一个案件
    assert second.case_no == first.case_no
    assert any("合并到案件" in note for note in second.response["notes"])
    case_row = gw_db.execute(select(RcCase)).scalar_one()
    assert case_row.event_cnt == 2
    assert case_row.hit_cnt == 2


def test_pass_does_not_create_case(gw_db: Session) -> None:
    """通过的事件不建案（否则人工队列会被低风险流量淹没）。"""
    result = event_gateway.handle_event(
        gw_db, event=make_event(event_type="login", event_id="EVT-PASS-1")
    )
    assert result.response["action"] == "Pass"
    assert result.case_no is None
    assert gw_db.execute(select(func.count()).select_from(RcCase)).scalar_one() == 0



def test_rule_score_is_capped_at_100(gw_db: Session) -> None:
    """多条规则叠加超过 100 时综合分必须封顶 100（否则 risk_score 会越过阈值语义）。"""
    add_rule(gw_db, code="RC-T-A", text="user_login_cnt_24h >= 1", score=60)
    add_rule(gw_db, code="RC-T-B", text="user_login_cnt_24h >= 1", score=60)
    result = event_gateway.handle_event(gw_db, event=make_event(event_type="login"))

    assert result.response["rule_score"] == 100
    assert result.response["risk_score"] == 100
    assert result.response["action"] == "Reject"


def test_blacklist_hit_skips_rules_and_models(gw_db: Session) -> None:
    """黑名单直判：decided_by=list、规则分与模型分归零、仍产出特征快照。"""
    add_rule(gw_db, code="RC-T-ANY", text="user_login_cnt_24h >= 1", score=40)
    gw_db.add(
        RcListEntry(
            list_type=LIST_BLACK,
            dimension=DIM_USER,
            value="U-BLACK",
            reason="批量注册账号",
            priority=10,
            status=STATUS_ACTIVE,
        )
    )
    gw_db.commit()
    list_service.invalidate_cache()

    result = event_gateway.handle_event(gw_db, event=make_event(event_type="login", user_id="U-BLACK"))

    assert result.response["action"] == "Reject"
    assert result.response["decided_by"] == "list"
    assert result.response["rule_score"] == 0
    assert result.response["model_score"] == 0
    assert result.response["risk_score"] == 100
    assert count_rows(gw_db, RcDecisionHit) == 0
    assert result.response["list_hits"][0]["list_type"] == "black"
    # 名单直判也要有特征快照，否则案件详情里只有一句"命中黑名单"
    assert result.response["features"]["subject_blacklist"] is True


def test_whitelist_hit_direct_pass(gw_db: Session) -> None:
    """白名单直判 Pass：即使规则会命中也不执行（白名单是强放行）。"""
    add_rule(gw_db, code="RC-T-ANY2", text="user_login_cnt_24h >= 1", score=90)
    gw_db.add(
        RcListEntry(
            list_type=LIST_WHITE,
            dimension=DIM_USER,
            value="U-WHITE",
            reason="内部测试账号",
            priority=10,
            status=STATUS_ACTIVE,
        )
    )
    gw_db.commit()
    list_service.invalidate_cache()

    result = event_gateway.handle_event(gw_db, event=make_event(event_type="login", user_id="U-WHITE"))
    assert result.response["action"] == "Pass"
    assert result.response["decided_by"] == "list"
    assert result.response["risk_score"] == 0


def test_five_event_types_all_persist(gw_db: Session) -> None:
    """验收标准 1：五类事件各注入一条，全部解析、落库、产出决策。"""
    gw_db.add(BizCustomer(user_id="U1", phone="13800000001", register_at=utcnow() - timedelta(days=30)))
    gw_db.add(BizOrder(order_no="SO-1", user_id="U1", product_id="P-1", quantity=1, amount=199))
    gw_db.commit()

    seen: list[str] = []
    for event_type in ("login", "coupon_receive", "order_create", "order_pay", "after_sale_apply"):
        result = event_gateway.handle_event(gw_db, event=make_event(event_type=event_type))
        assert result.response["event_type"] == event_type
        assert result.response["decision_id"].startswith("D")
        seen.append(event_type)

    assert count_rows(gw_db, RcEvent) == 5
    assert count_rows(gw_db, RcDecision) == 5
    assert count_rows(gw_db, RcFeatureSnapshot) == 5
    stored = {row.event_type for row in gw_db.execute(select(RcEvent)).scalars().all()}
    assert stored == set(seen)

    # 事件类型专有 payload 必须原样落库（可回放的物理基础）
    refund = gw_db.execute(select(RcEvent).where(RcEvent.event_type == "after_sale_apply")).scalar_one()
    assert refund.payload["refund_no"] == "RF-1"
    assert refund.biz_no == "RF-1"


def test_cluster_strips_feed_distinct_features(gw_db: Session) -> None:
    """同设备多账号必须能被聚集度特征读到（条带 member 格式与读取口径成对生效）。

    这是最容易静默失效的一处：写 member 时若丢了 ``user:{id}`` 前缀，
    ``device_account_cnt_24h`` 会恒为 0，且没有任何异常。
    """
    for index in range(3):
        event_gateway.handle_event(
            gw_db,
            event=make_event(
                event_type="login",
                event_id=f"EVT-CLUSTER-{index}",
                user_id=f"U-C{index}",
                device_id="DEV-FARM",
                phone=f"1390000000{index}",
            ),
        )

    result = event_gateway.handle_event(
        gw_db,
        event=make_event(
            event_type="login",
            event_id="EVT-CLUSTER-LAST",
            user_id="U-C3",
            device_id="DEV-FARM",
            phone="13900000009",
        ),
    )
    assert result.response["features"]["device_account_cnt_24h"] == 4
    # 同设备的登录次数也要计对：聚簇条带不能把"事件条数"与"主体去重"混在一个键里
    assert result.response["features"]["user_login_cnt_24h"] == 1


def test_time_skew_warning_does_not_reject(gw_db: Session) -> None:
    """时间偏差只告警不拒绝（PRD §6.3）。"""
    future = utcnow() + timedelta(minutes=30)
    result = event_gateway.handle_event(
        gw_db, event=make_event(event_type="login", occurred_at=future)
    )
    assert result.accepted is True
    assert any("超出容忍阈值" in warning for warning in result.response["warnings"])


def test_cluster_strip_still_counts_events_correctly(gw_db: Session) -> None:
    """聚簇实体上的计数特征必须等于事件条数（不能因为带主体标识而折叠成员）。

    这类失效是静默的：若聚簇条带按"每个主体只写一个成员"来写，
    同一用户在同一设备上的 4 次领券会算成 1 次，
    ``device_coupon_cnt_1h > 5`` 这类规则永远不会命中，且没有任何报错。
    """
    for index in range(3):
        event_gateway.handle_event(
            gw_db,
            event=make_event(
                event_type="coupon_receive",
                event_id=f"EVT-COUNT-{index}",
                user_id="U-COUNT",
                device_id="DEV-COUNT",
                ip="10.8.8.8",
            ),
        )
    result = event_gateway.handle_event(
        gw_db,
        event=make_event(
            event_type="coupon_receive",
            event_id="EVT-COUNT-LAST",
            user_id="U-COUNT",
            device_id="DEV-COUNT",
            ip="10.8.8.8",
        ),
    )

    assert result.response["features"]["device_coupon_cnt_1h"] == 4
    assert result.response["features"]["ip_coupon_cnt_1h"] == 4
    assert result.response["features"]["user_coupon_cnt_1h"] == 4
    # 去重维度：同一个用户只算一个主体
    assert result.response["features"]["device_account_cnt_24h"] == 1


def test_distinct_account_cnt_merges_event_types(gw_db: Session) -> None:
    """同设备账号数必须跨事件类型合并去重，而不是各类型相加。

    同一账号既有登录又有领券时，逐类型相加会把它数两次
    （"同设备 3 个账号"变成 6 个），阈值随即静默失效。
    """
    event_gateway.handle_event(
        gw_db, event=make_event(event_type="login", event_id="EVT-MIX-1", user_id="U-MIX", device_id="DEV-MIX")
    )
    event_gateway.handle_event(
        gw_db,
        event=make_event(event_type="coupon_receive", event_id="EVT-MIX-2", user_id="U-MIX", device_id="DEV-MIX"),
    )
    result = event_gateway.handle_event(
        gw_db, event=make_event(event_type="login", event_id="EVT-MIX-3", user_id="U-MIX", device_id="DEV-MIX")
    )

    assert result.response["features"]["device_account_cnt_24h"] == 1
    assert result.response["features"]["device_account_cnt_7d"] == 1


# --------------------------------------------------------------------------- #
# 接口层
# --------------------------------------------------------------------------- #
def _seed_api_key(db: Session, raw: str = "test-api-key-123456") -> None:
    db.add(SysApiKey(name="测试业务端", api_key_hash=hash_api_key(raw), enabled=True))
    db.commit()


def test_event_api_requires_api_key(client: TestClient) -> None:
    response = client.post("/api/v1/events", json=make_event().model_dump(mode="json"))
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "40104"
    assert body["field"] == "X-API-Key"
    assert body["trace_id"]


def test_event_api_rejects_unknown_key(client: TestClient) -> None:
    response = client.post(
        "/api/v1/events",
        json=make_event().model_dump(mode="json"),
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "40105"


def test_event_api_accepts_event_with_valid_key(client: TestClient, gw_db: Session) -> None:
    _seed_api_key(gw_db)
    response = client.post(
        "/api/v1/events",
        json=make_event(event_type="coupon_receive").model_dump(mode="json"),
        headers={"X-API-Key": "test-api-key-123456"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "0"
    assert body["data"]["accepted"] is True
    assert body["data"]["decision"]["action"] in {"Pass", "Review", "Reject", "Challenge"}
    assert response.headers["X-Trace-Id"]


def test_event_api_missing_required_payload_field(client: TestClient, gw_db: Session) -> None:
    """业务校验失败要给出字段路径（前端可直接挂到表单项上）。"""
    _seed_api_key(gw_db)
    bad = make_event(event_type="coupon_receive", payload={"coupon_id": "CP-9"})
    response = client.post(
        "/api/v1/events",
        json=bad.model_dump(mode="json"),
        headers={"X-API-Key": "test-api-key-123456"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "40004"
    assert body["field"] == "payload.face_value"
    assert count_rows(gw_db, RcEvent) == 0


def test_event_api_batch_partial_failure(client: TestClient, gw_db: Session) -> None:
    """批量：一条成功一条失败，HTTP 仍 200，失败原因逐条给出。"""
    _seed_api_key(gw_db)
    good = make_event(event_type="login", event_id="EVT-BATCH-OK")
    bad = make_event(event_type="order_pay", event_id="EVT-BATCH-BAD", payload={"order_no": "SO-NOT-EXIST", "pay_amount": 1})
    response = client.post(
        "/api/v1/events/batch",
        json={"events": [good.model_dump(mode="json"), bad.model_dump(mode="json")]},
        headers={"X-API-Key": "test-api-key-123456"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert data["accepted"] == 1
    assert len(data["errors"]) == 1
    assert data["errors"][0]["index"] == 1
    assert data["errors"][0]["event_id"] == "EVT-BATCH-BAD"
    assert data["errors"][0]["code"] == "40402"
    assert count_rows(gw_db, RcEvent) == 1


def test_login_api_and_audit_trail(client: TestClient, gw_db: Session) -> None:
    """登录成功返回 JWT 并写审计；口令错误返回统一错误码且不泄露账号是否存在。"""
    gw_db.add(
        SysUser(
            username="admin",
            password_hash=hash_password("admin123"),
            real_name="管理员",
            role=ROLE_ADMIN,
            status=STATUS_ENABLED,
        )
    )
    gw_db.commit()

    ok = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert ok.status_code == 200
    data = ok.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["user"]["role"] == ROLE_ADMIN

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"})
    assert me.status_code == 200
    assert me.json()["data"]["username"] == "admin"

    bad = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert bad.status_code == 401
    assert bad.json()["code"] == "40107"

    unknown = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "whatever"})
    assert unknown.status_code == 401
    # 账号不存在与口令错误返回同一错误码：不给出账号枚举的旁路
    assert unknown.json()["code"] == bad.json()["code"]

    from app.models.audit import RcAuditLog

    assert count_rows(gw_db, RcAuditLog) == 1


def test_me_requires_token(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "40101"
