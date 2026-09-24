"""案件服务测试：建案、合案、状态机、列表与详情（docs/PRD.md §9.1/§9.2/§11.3）。

覆盖的风险按重要性排序：

1. **合案边界**：窗口内合并、窗口外新建、跨场景不合、已处置不合 ——
   任何一侧错了都会让工作台出现"同一用户两个案件"或"一次触发一个案件"。
2. **计数与等级单调**：hit_cnt / event_cnt 累加、max_score 取最大、
   risk_level 取更高的一档（不能被后来的低分事件降级）。
3. **状态机并发正确性**：接手用条件更新，第二个请求必须拿 409 且看到当前处理人。
4. **批量归档的逐条语义**：状态不符的条目只跳过，不影响同一批里的其它条目。
5. **详情组装**：证据链、特征分组、画像、单据、图谱都要真的取到数据
   （前端三栏全依赖它，少一块就是"页面半空"）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.timeutil import utcnow
from app.models.biz import CUSTOMER_NORMAL, ORDER_CREATED, BizCustomer, BizOrder
from app.models.case import (
    CASE_ARCHIVED,
    CASE_CLOSED,
    CASE_DISPOSED,
    CASE_PENDING,
    CASE_PROCESSING,
    RcCase,
    RcCaseEvent,
)
from app.models.decision import RcDecision, RcDecisionHit
from app.models.event import RcFeatureSnapshot
from app.models.sys import SysUser
from app.services import case_service
from app.services.errors import ConflictError, ErrorCode, NotFoundError
from tests.case_fixtures import (  # noqa: F401 - 夹具需导入进模块命名空间才能被 pytest 发现
    actor_of,
    admin,
    audit_actions,
    auditor,
    case_db,
    count,
    make_case,
    make_event_row,
    other_auditor,
)

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# 建案与合案
# --------------------------------------------------------------------------- #
def test_create_case_writes_case_event_and_audit(case_db) -> None:
    """首次触发建案：落 rc_case + rc_case_event，并把系统建案写进审计。"""
    result = make_case(case_db)

    assert result.created is True
    assert result.case_no.startswith("C")
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.status == CASE_PENDING
    assert row.subject_value == "U1"
    assert row.scene == "coupon"
    assert row.max_score == 85
    assert row.risk_level == "high"
    assert row.event_cnt == 1
    assert row.hit_cnt == 2
    assert row.handler is None
    assert row.last_event_id == "EVT-1"
    assert count(case_db, RcCaseEvent) == 1
    assert "case_create" in audit_actions(case_db)


def test_merge_within_window_accumulates(case_db) -> None:
    """窗口内同主体同场景 -> 合并：计数累加、取最高分、取更高风险等级。"""
    first = make_case(case_db, risk_score=85, risk_level="high", hit_count=2, event_id="EVT-1")
    second = make_case(
        case_db,
        risk_score=70,
        risk_level="mid",
        hit_count=1,
        event_id="EVT-2",
        decision_id="D-2",
        occurred_at=utcnow() + timedelta(minutes=5),
    )

    assert second.created is False
    assert second.case_no == first.case_no
    assert count(case_db, RcCase) == 1
    assert count(case_db, RcCaseEvent) == 2
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.event_cnt == 2
    assert row.hit_cnt == 3
    assert row.max_score == 85
    # 后来的低分事件不能把案件等级降级
    assert row.risk_level == "high"
    assert row.last_event_id == "EVT-2"
    assert row.last_decision_id == "D-2"


def test_merge_raises_level_when_new_event_is_worse(case_db) -> None:
    """反向：先 mid 后 high 时必须升级为 high（等级只能升不能降）。"""
    make_case(case_db, risk_score=65, risk_level="mid", event_id="EVT-1")
    make_case(case_db, risk_score=90, risk_level="high", event_id="EVT-2", decision_id="D-2")

    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.risk_level == "high"
    assert row.max_score == 90


def test_merge_outside_window_creates_new_case(case_db) -> None:
    """窗口（默认 30 分钟）之外的同主体同场景事件 -> 新建案件。"""
    first = make_case(case_db, event_id="EVT-1")
    later = make_case(
        case_db,
        event_id="EVT-2",
        decision_id="D-2",
        occurred_at=utcnow() + timedelta(minutes=31),
    )

    assert later.created is True
    assert later.case_no != first.case_no
    assert count(case_db, RcCase) == 2


def test_different_scene_is_not_merged(case_db) -> None:
    """同主体不同场景 -> 各自建案（处置对象不同，合起来会指错单据）。"""
    coupon = make_case(case_db, scene="coupon", event_id="EVT-1")
    order = make_case(case_db, scene="order", event_id="EVT-2", decision_id="D-2")

    assert order.case_no != coupon.case_no
    assert count(case_db, RcCase) == 2


def test_disposed_case_is_not_merged(case_db, auditor: SysUser) -> None:
    """已处置的案件不再接受合案：处置结论不能被后续证据混进同一个案件。"""
    from app.services import disposal_service

    first = make_case(case_db, event_id="EVT-1")
    case_service.claim(case_db, case_no=first.case_no, actor=actor_of(auditor))
    disposal_service.dispose(
        case_db,
        case_no=first.case_no,
        actor=actor_of(auditor),
        business_result="reject",
        risk_actions=["pass"],
        remark="确认是羊毛党批量领券行为，本次驳回处理",
    )

    again = make_case(case_db, event_id="EVT-2", decision_id="D-2")
    assert again.created is True
    assert again.case_no != first.case_no
    assert count(case_db, RcCase) == 2


def test_merge_does_not_move_last_at_backwards(case_db) -> None:
    """补投递的旧事件不能让 last_at 倒退（否则下一次合案会凭空多出一个案件）。"""
    first = make_case(case_db, event_id="EVT-1")
    before = case_db.execute(select(RcCase.last_at)).scalar_one()

    case_service.merge_or_create(
        case_db,
        subject_value="U1",
        scene="coupon",
        event_id="EVT-OLD",
        decision_id="D-OLD",
        event_type="coupon_receive",
        action="Reject",
        risk_level="high",
        risk_score=85,
        hit_count=1,
        biz_no=None,
        occurred_at=utcnow() - timedelta(minutes=10),
    )
    case_db.commit()

    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.case_no == first.case_no
    assert row.event_cnt == 2
    assert row.last_at == before


def test_merge_window_config_is_honored(case_db) -> None:
    """合案窗口读 sys_config：把窗口调到 1 分钟后，6 分钟前的事件就该另立案件。

    这条测试防的是"配置项写了但代码没读"——那种情况下 30 分钟硬编码也能跑通
    所有其它用例，只有把窗口改小才暴露。
    """
    from app.models.sys import SysConfig

    case_db.add(
        SysConfig(config_key="case_merge_window_minutes", config_value="1", value_type="int")
    )
    case_db.commit()
    from app.services import config_service

    config_service.invalidate_cache()

    first = make_case(case_db, event_id="EVT-1")
    later = make_case(
        case_db,
        event_id="EVT-2",
        decision_id="D-2",
        occurred_at=utcnow() + timedelta(minutes=6),
    )
    assert later.created is True
    assert later.case_no != first.case_no


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_claim_transitions_and_records_handler(case_db, auditor: SysUser) -> None:
    """接手：pending -> processing，记录处理人与时间，并写审计。"""
    result = make_case(case_db)
    row = case_service.claim(case_db, case_no=result.case_no, actor=actor_of(auditor))

    assert row.status == CASE_PROCESSING
    assert row.handler == "auditor1"
    assert row.handler_id == auditor.id
    assert row.claimed_at is not None
    assert "case_claim" in audit_actions(case_db)


def test_claim_conflict_reports_current_handler(
    case_db, auditor: SysUser, other_auditor: SysUser
) -> None:
    """已被他人接手时，第二个接手请求必须 409 并告知当前处理人。"""
    result = make_case(case_db)
    case_service.claim(case_db, case_no=result.case_no, actor=actor_of(auditor))

    with pytest.raises(ConflictError) as exc:
        case_service.claim(case_db, case_no=result.case_no, actor=actor_of(other_auditor))

    assert exc.value.code == ErrorCode.STATE_CONFLICT
    assert exc.value.detail["handler"] == "auditor1"
    assert "auditor1" in exc.value.message
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.handler == "auditor1"


def test_claim_missing_case_returns_404(case_db, auditor: SysUser) -> None:
    """接手不存在的案件 -> 40404（不是 409：它压根不存在）。"""
    with pytest.raises(NotFoundError) as exc:
        case_service.claim(case_db, case_no="C-NOT-EXIST", actor=actor_of(auditor))
    assert exc.value.code == ErrorCode.CASE_NOT_FOUND


def test_archive_only_disposed_and_reports_each_case(
    case_db, auditor: SysUser, admin: SysUser
) -> None:
    """批量归档：已处置的成功、待审的被跳过并给出原因。"""
    from app.services import disposal_service

    disposed = make_case(case_db, event_id="EVT-1")
    case_service.claim(case_db, case_no=disposed.case_no, actor=actor_of(auditor))
    disposal_service.dispose(
        case_db,
        case_no=disposed.case_no,
        actor=actor_of(auditor),
        business_result="reject",
        risk_actions=["pass"],
        remark="确认作弊行为，处置完成后等待归档",
    )
    pending = make_case(case_db, user_id="U2", event_id="EVT-2", decision_id="D-2")

    results = case_service.archive(
        case_db, case_nos=[disposed.case_no, pending.case_no, "C-NOPE"], actor=actor_of(admin)
    )
    by_no = {item["case_no"]: item for item in results}
    assert by_no[disposed.case_no]["ok"] is True
    assert by_no[pending.case_no]["ok"] is False
    assert "pending" in by_no[pending.case_no]["reason"]
    assert by_no["C-NOPE"]["ok"] is False
    assert "不存在" in by_no["C-NOPE"]["reason"]

    rows = {row.case_no: row for row in case_db.execute(select(RcCase)).scalars().all()}
    assert rows[disposed.case_no].status == CASE_ARCHIVED
    assert rows[disposed.case_no].archived_at is not None
    assert rows[pending.case_no].status == CASE_PENDING
    assert "case_archive" in audit_actions(case_db)


def test_close_requires_open_case(case_db, auditor: SysUser, admin: SysUser) -> None:
    """强制关闭：未结案件可关闭并记原因；已处置的不能再关闭。"""
    from app.services import disposal_service

    result = make_case(case_db, event_id="EVT-1")
    row = case_service.close(
        case_db, case_no=result.case_no, actor=actor_of(admin), reason="重复建案，人工关闭"
    )
    assert row.status == CASE_CLOSED
    assert row.close_reason == "重复建案，人工关闭"
    assert row.closed_at is not None

    done = make_case(case_db, user_id="U2", event_id="EVT-2", decision_id="D-2")
    case_service.claim(case_db, case_no=done.case_no, actor=actor_of(auditor))
    disposal_service.dispose(
        case_db,
        case_no=done.case_no,
        actor=actor_of(auditor),
        business_result="approve",
        risk_actions=["pass"],
        remark="核对后确认是正常用户的一次误报",
    )
    with pytest.raises(ConflictError):
        case_service.close(case_db, case_no=done.case_no, actor=actor_of(admin), reason="再关一次")


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def test_list_cases_filters_and_counts(case_db, auditor: SysUser) -> None:
    """列表筛选：场景、关键词、只看我的案件；状态计数分组正确。"""
    coupon = make_case(case_db, user_id="U1", scene="coupon", event_id="EVT-1")
    make_case(case_db, user_id="U2", scene="order", event_id="EVT-2", decision_id="D-2")
    case_service.claim(case_db, case_no=coupon.case_no, actor=actor_of(auditor))

    rows, total = case_service.list_cases(
        case_db, query=case_service.CaseQuery(scene="order"), page_no=1, size=10
    )
    assert total == 1
    assert rows[0].scene == "order"

    rows, total = case_service.list_cases(
        case_db, query=case_service.CaseQuery(keyword="U1"), page_no=1, size=10
    )
    assert total == 1
    assert rows[0].subject_value == "U1"

    rows, total = case_service.list_cases(
        case_db,
        query=case_service.CaseQuery(handler_id=auditor.id),
        page_no=1,
        size=10,
    )
    assert total == 1
    assert rows[0].case_no == coupon.case_no

    rows, total = case_service.list_cases(
        case_db,
        query=case_service.CaseQuery(status=CASE_PENDING, risk_level="high"),
        page_no=1,
        size=10,
    )
    assert total == 1
    assert rows[0].case_no != coupon.case_no

    counts = case_service.status_counts(case_db)
    assert counts[CASE_PENDING] == 1
    assert counts[CASE_PROCESSING] == 1
    assert counts[CASE_DISPOSED] == 0


def test_list_cases_pagination_is_stable(case_db) -> None:
    """同秒批量案件的分页不重不漏（last_at 相同靠 id 兜底成全序）。"""
    occurred = utcnow()
    for index in range(5):
        make_case(
            case_db,
            user_id=f"U{index}",
            event_id=f"EVT-{index}",
            decision_id=f"D-{index}",
            occurred_at=occurred,
            commit=False,
        )
    case_db.commit()

    seen: list[str] = []
    for page in (1, 2, 3):
        rows, total = case_service.list_cases(
            case_db, query=case_service.CaseQuery(), page_no=page, size=2
        )
        assert total == 5
        seen.extend(row.case_no for row in rows)
    assert len(seen) == len(set(seen)) == 5


# --------------------------------------------------------------------------- #
# 详情组装
# --------------------------------------------------------------------------- #
def test_case_detail_assembles_evidence(case_db) -> None:
    """详情包含事件时间线、命中规则、特征分组、画像、单据与图谱。"""
    make_event_row(case_db, event_id="EVT-1", event_type="order_create", biz_no="ORD-9")
    case_db.add(
        BizOrder(
            order_no="ORD-9",
            user_id="U1",
            product_id="P1",
            quantity=2,
            amount=399.0,
            status=ORDER_CREATED,
        )
    )
    case_db.add(BizCustomer(user_id="U1", phone="13800000001", status=CUSTOMER_NORMAL))
    case_db.add(
        RcDecision(
            decision_id="D-1",
            event_id="EVT-1",
            rule_score=85,
            model_score=20,
            risk_score=91,
            risk_level="high",
            action="Reject",
            action_hint="none",
            decided_by="rule_model",
            fusion_alpha=0.3,
            fusion_mode="additive",
            hit_count=1,
            latency_ms=12,
            from_cache=False,
        )
    )
    case_db.add(
        RcDecisionHit(
            decision_id="D-1",
            rule_code="RC-001",
            rule_name="同设备多账号下单",
            rule_category="frequency",
            score=85,
            reason="同设备 24h 关联 7 个账号",
            evidence=[{"field": "device_account_cnt_24h", "op": ">=", "value": 5}],
        )
    )
    case_db.add(
        RcFeatureSnapshot(
            event_id="EVT-1",
            features={
                "device_account_cnt_24h": 7,
                "user_order_cnt_24h": 3,
                "subject_gray_flag": 0,
            },
            window_profile="1h+24h+7d",
            calc_cost_ms=8,
            feature_version="v1",
        )
    )
    case_db.commit()
    result = make_case(case_db, scene="order", event_type="order_create", biz_no="ORD-9")

    detail = case_service.build_case_detail(
        case_db, case_service.get_case(case_db, result.case_no)
    )

    assert detail["case"]["case_no"] == result.case_no
    assert len(detail["events"]) == 1
    assert detail["events"][0]["event_type"] == "order_create"
    focus = detail["focus"]
    assert focus["decision"]["risk_score"] == 91
    assert focus["hit_rules"][0]["rule_code"] == "RC-001"
    assert focus["hit_rules"][0]["evidence"][0]["field"] == "device_account_cnt_24h"
    groups = {group["group"] for group in focus["features"]}
    assert "聚集度" in groups
    assert focus["event_context"]["event"]["biz_no"] == "ORD-9"
    assert detail["biz_doc"]["order_no"] == "ORD-9"
    assert detail["biz_doc"]["status"] == ORDER_CREATED
    assert detail["profile"]["customer"]["user_id"] == "U1"
    assert detail["profile"]["account_age_days"] is not None
    assert detail["graph"]["nodes"][0]["is_center"] is True


def test_case_graph_links_same_device_and_ip_accounts(case_db) -> None:
    """图谱：同设备/同 IP 的其它账号成为邻居节点，并标注关系类型。"""
    make_event_row(case_db, event_id="EVT-A", user_id="U1", device_id="DEV-1", ip="10.0.0.1")
    make_event_row(case_db, event_id="EVT-B", user_id="U2", device_id="DEV-1", ip="10.0.0.9")
    make_event_row(case_db, event_id="EVT-C", user_id="U3", device_id="DEV-9", ip="10.0.0.1")
    result = make_case(case_db, user_id="U1", event_id="EVT-A", decision_id="D-A")

    detail = case_service.build_case_detail(
        case_db, case_service.get_case(case_db, result.case_no)
    )

    node_ids = {node["id"] for node in detail["graph"]["nodes"]}
    assert node_ids == {"U1", "U2", "U3"}
    edges = {(edge["source"], edge["target"], edge["label"]) for edge in detail["graph"]["edges"]}
    assert ("U1", "U2", "device") in edges
    assert ("U1", "U3", "ip") in edges


def test_case_detail_marks_list_status(case_db, auditor: SysUser) -> None:
    """画像卡展示本案主体当前的名单状态（处置后立刻可见）。"""
    from app.models.rclist import DIM_USER, LIST_BLACK, RcListEntry
    from app.services import disposal_service

    result = make_case(case_db)
    case_service.claim(case_db, case_no=result.case_no, actor=actor_of(auditor))
    disposal_service.dispose(
        case_db,
        case_no=result.case_no,
        actor=actor_of(auditor),
        business_result="reject",
        risk_actions=["blacklist_user"],
        remark="确认恶意账号，拉入黑名单并留档",
    )
    entry = case_db.execute(select(RcListEntry)).scalar_one()
    assert entry.list_type == LIST_BLACK and entry.dimension == DIM_USER

    detail = case_service.build_case_detail(
        case_db, case_service.get_case(case_db, result.case_no)
    )
    assert detail["profile"]["list_status"][0]["list_type"] == LIST_BLACK
    assert detail["case"]["status"] == CASE_DISPOSED


def test_subject_case_cnt_reads_real_cases(case_db) -> None:
    """``subject_case_cnt`` 已接真实案件表（P0 时期恒为 0 的简化项）。

    这项特征是"这个账号被风控盯上过几次"的强度信号。P0 期它恒为 0 并记
    missing_fields —— 引用它的规则因此永远不触发，而规则页面上看不出任何
    异常。P1 接上案件表后，同一个规则"无需改动就开始工作"，本测试锁住这条
    契约：没有它，将来有人把这次接入回退成常量，不会有任何测试失败。
    """
    from app.services import feature_engine

    make_case(
        case_db,
        user_id="U1",
        scene="coupon",
        event_id="EVT-1",
        occurred_at=utcnow() - timedelta(days=40),
    )
    make_case(
        case_db,
        user_id="U1",
        scene="coupon",
        event_id="EVT-2",
        decision_id="D-2",
        occurred_at=utcnow() - timedelta(days=20),
    )

    result = feature_engine.compute(
        case_db, event_type="coupon_receive", user_id="U1", occurred_at=utcnow()
    )
    assert result.features["subject_case_cnt"] == 2
    assert "subject_case_cnt" not in result.missing_fields
