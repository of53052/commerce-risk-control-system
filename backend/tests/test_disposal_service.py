"""处置服务测试：入参校验、权限、状态前置与五类联动（docs/PRD.md §9.3/§9.4）。

覆盖的风险按重要性排序：

1. **入参校验**：备注 ≥10 字、业务结论取值、动作非空/合法/pass 互斥 ——
   校验失败必须不改状态（否则案件会卡在一个无法再处置的状态）。
2. **权限与状态前置**：非当前处理人 40302；未接手不能处置；
   admin 例外可兜底；重复处置 409。
3. **联动真的落到业务表**：订单取消、退款驳回、名单写入、业务用户置黑。
   PRD §17.2 验收标准 5 要求"处置后业务单据状态校验"，只写日志不算通过。
4. **联动失败不阻塞**：已支付订单不可取消时，处置本身必须成功返回，
   联动项记 skipped 并带上原因（前端逐项展示）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.biz import (
    CUSTOMER_BLACKLISTED,
    CUSTOMER_NORMAL,
    ORDER_CANCELLED,
    ORDER_CREATED,
    ORDER_PAID,
    REFUND_APPLIED,
    REFUND_REJECTED,
    BizCustomer,
    BizOrder,
    BizRefund,
)
from app.models.case import (
    CASE_DISPOSED,
    CASE_PROCESSING,
    RcCase,
    RcCaseAction,
    RcCaseActionItem,
)
from app.models.rclist import DIM_DEVICE, DIM_USER, LIST_BLACK, LIST_GRAY, RcListEntry
from app.models.sys import SysUser
from app.services import case_service, disposal_service
from app.services.errors import ConflictError, ErrorCode, PermissionError_, ValidationError
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

_GOOD_REMARK = "证据链完整，确认存在团伙化作弊行为"


def claimed_case(db, auditor: SysUser, **kwargs):
    """建案并接手（大多数处置测试的前置状态）。"""
    result = make_case(db, **kwargs)
    case_service.claim(db, case_no=result.case_no, actor=actor_of(auditor))
    return result


def dispose(db, case_no: str, actor: SysUser, **kwargs):
    """按默认参数提交处置，测试只覆盖关心的字段。"""
    payload = {
        "business_result": "reject",
        "risk_actions": ["pass"],
        "remark": _GOOD_REMARK,
    }
    payload.update(kwargs)
    return disposal_service.dispose(
        db,
        case_no=case_no,
        actor=actor_of(actor),
        business_result=payload["business_result"],
        risk_actions=payload["risk_actions"],
        remark=payload["remark"],
    )


# --------------------------------------------------------------------------- #
# 入参校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "business_result,risk_actions,remark,field",
    [
        ("reject", ["pass"], "太短", "remark"),
        ("reject", ["pass"], "   ", "remark"),
        ("pass", ["pass"], "业务结论写成了非法取值的提交", "business_result"),
        ("reject", [], "一个风控动作都没勾选的处置提交", "risk_actions"),
        ("reject", ["not_an_action"], "动作名拼错的处置提交测试", "risk_actions"),
        ("reject", ["pass", "block_order"], "pass 与拦截动作混选的非法提交", "risk_actions"),
    ],
)
def test_dispose_rejects_invalid_payload(
    case_db, auditor: SysUser, business_result, risk_actions, remark, field
) -> None:
    """入参校验：备注长度、结论取值、动作非空/合法/互斥，且不改状态。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    with pytest.raises(ValidationError) as exc:
        disposal_service.dispose(
            case_db,
            case_no=result.case_no,
            actor=actor_of(auditor),
            business_result=business_result,
            risk_actions=risk_actions,
            remark=remark,
        )
    assert exc.value.code == ErrorCode.PARAM_INVALID
    assert exc.value.field == field
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.status == CASE_PROCESSING
    assert count(case_db, RcCaseAction) == 0


def test_dispose_trims_remark_before_length_check(case_db, auditor: SysUser) -> None:
    """备注先 strip 再判长度：首尾空白不算字数（否则"     加九个字"能绕过门槛）。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")
    with pytest.raises(ValidationError):
        dispose(
            case_db,
            result.case_no,
            auditor,
            remark="       短备注         ",
        )


# --------------------------------------------------------------------------- #
# 权限与状态前置
# --------------------------------------------------------------------------- #
def test_dispose_requires_current_handler(
    case_db, auditor: SysUser, other_auditor: SysUser
) -> None:
    """非当前处理人提交处置 -> 40302。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    with pytest.raises(PermissionError_) as exc:
        dispose(case_db, result.case_no, other_auditor)
    assert exc.value.code == ErrorCode.CASE_HANDLER_MISMATCH
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.status == CASE_PROCESSING


def test_dispose_pending_case_requires_claim(case_db, auditor: SysUser) -> None:
    """未接手（pending）的普通审核员不能直接处置：要先"看过证据"。"""
    result = make_case(case_db, event_id="EVT-1")

    with pytest.raises(ConflictError) as exc:
        dispose(case_db, result.case_no, auditor)
    assert exc.value.code == ErrorCode.STATE_CONFLICT


def test_admin_can_dispose_pending_case(case_db, admin: SysUser) -> None:
    """admin 兜底：允许直接在 pending 上处置，并记为自己的处置。"""
    result = make_case(case_db, event_id="EVT-1")
    outcome = dispose(case_db, result.case_no, admin)

    assert outcome.items[0]["exec_result"] == "success"
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.status == CASE_DISPOSED
    assert row.dispose_result == "reject"
    assert row.handler == "admin1"
    assert row.disposed_at is not None


def test_dispose_twice_conflicts(case_db, auditor: SysUser) -> None:
    """重复处置 -> 409（同一个案件不能落两次结论）。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")
    dispose(case_db, result.case_no, auditor)

    with pytest.raises(ConflictError) as exc:
        dispose(case_db, result.case_no, auditor, business_result="approve")
    assert exc.value.code == ErrorCode.STATE_CONFLICT
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.dispose_result == "reject"


def test_dispose_records_action_and_audit(case_db, auditor: SysUser) -> None:
    """处置落 rc_case_action（双维度 + 备注）并写审计（含处置原因）。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    outcome = dispose(
        case_db,
        result.case_no,
        auditor,
        risk_actions=["block_order", "blacklist_user"],
        remark="拦截订单并拉黑账号，证据链完整",
    )

    action = case_db.execute(select(RcCaseAction)).scalar_one()
    assert action.business_result == "reject"
    assert action.risk_actions == ["block_order", "blacklist_user"]
    assert action.remark == "拦截订单并拉黑账号，证据链完整"
    assert action.operator_name == "auditor1"
    assert action.status_before == CASE_PROCESSING
    assert action.status_after == CASE_DISPOSED
    assert outcome.action_id == action.id
    assert count(case_db, RcCaseActionItem) == 2
    assert "case_dispose" in audit_actions(case_db)


def test_dispose_deduplicates_actions_keeping_order(case_db, auditor: SysUser) -> None:
    """重复勾选同一动作时去重且保序（回显顺序与提交顺序一致）。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")
    outcome = dispose(
        case_db,
        result.case_no,
        auditor,
        risk_actions=["watchlist_add", "blacklist_user", "watchlist_add"],
    )
    assert outcome.risk_actions == ["watchlist_add", "blacklist_user"]
    action = case_db.execute(select(RcCaseAction)).scalar_one()
    assert action.risk_actions == ["watchlist_add", "blacklist_user"]


# --------------------------------------------------------------------------- #
# 处置联动
# --------------------------------------------------------------------------- #
def test_block_order_cancels_unpaid_order(case_db, auditor: SysUser) -> None:
    """block_order：未支付订单被取消，明细记录目标与前后状态。"""
    make_event_row(case_db, event_id="EVT-1", event_type="order_create", biz_no="ORD-1")
    case_db.add(
        BizOrder(
            order_no="ORD-1",
            user_id="U1",
            product_id="P1",
            quantity=1,
            amount=199.0,
            status=ORDER_CREATED,
        )
    )
    case_db.commit()
    result = claimed_case(
        case_db, auditor, scene="order", event_type="order_create", biz_no="ORD-1"
    )

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["block_order"])

    item = outcome.items[0]
    assert item["risk_action"] == "block_order"
    assert item["exec_result"] == "success"
    assert item["target_id"] == "ORD-1"
    assert item["detail"] == {"from": ORDER_CREATED, "to": ORDER_CANCELLED}
    order = case_db.execute(select(BizOrder)).scalar_one()
    assert order.status == ORDER_CANCELLED


def test_block_order_skips_paid_order(case_db, auditor: SysUser) -> None:
    """已支付订单不取消：联动记 skipped，但处置本身必须成功。"""
    make_event_row(case_db, event_id="EVT-1", event_type="order_create", biz_no="ORD-2")
    case_db.add(
        BizOrder(
            order_no="ORD-2",
            user_id="U1",
            product_id="P1",
            quantity=1,
            amount=199.0,
            status=ORDER_PAID,
        )
    )
    case_db.commit()
    result = claimed_case(
        case_db, auditor, scene="order", event_type="order_create", biz_no="ORD-2"
    )

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["block_order"])

    item = outcome.items[0]
    assert item["exec_result"] == "skipped"
    assert item["detail"]["status"] == ORDER_PAID
    order = case_db.execute(select(BizOrder)).scalar_one()
    assert order.status == ORDER_PAID
    row = case_db.execute(select(RcCase)).scalar_one()
    assert row.status == CASE_DISPOSED


def test_block_order_skips_when_no_order_linked(case_db, auditor: SysUser) -> None:
    """案件里没有订单号（只领券/只登录）时，block_order 记 skipped 而非 failed。"""
    result = claimed_case(case_db, auditor, scene="coupon", event_id="EVT-1")
    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["block_order"])
    assert outcome.items[0]["exec_result"] == "skipped"
    assert "无需拦截" in outcome.items[0]["detail"]["reason"]


def test_block_order_fails_when_order_missing(case_db, auditor: SysUser) -> None:
    """事件引用了不存在的订单号 -> failed（数据不一致，必须被看见）。"""
    make_event_row(case_db, event_id="EVT-1", event_type="order_create", biz_no="ORD-MISSING")
    result = claimed_case(
        case_db, auditor, scene="order", event_type="order_create", biz_no="ORD-MISSING"
    )
    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["block_order"])
    assert outcome.items[0]["exec_result"] == "failed"
    assert "不存在" in outcome.items[0]["detail"]["reason"]


def test_blacklist_user_writes_list_and_biz_customer(case_db, auditor: SysUser) -> None:
    """blacklist_user：写黑名单 + 业务用户置黑，两处都要落。"""
    case_db.add(BizCustomer(user_id="U1", phone="13800000001", status=CUSTOMER_NORMAL))
    case_db.commit()
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["blacklist_user"])

    assert outcome.items[0]["exec_result"] == "success"
    assert outcome.items[0]["detail"]["biz_customer_updated"] is True
    entry = case_db.execute(select(RcListEntry)).scalar_one()
    assert (entry.list_type, entry.dimension, entry.value) == (LIST_BLACK, DIM_USER, "U1")
    assert result.case_no in (entry.reason or "")
    customer = case_db.execute(select(BizCustomer)).scalar_one()
    assert customer.status == CUSTOMER_BLACKLISTED


def test_blacklist_user_without_biz_customer_still_writes_list(
    case_db, auditor: SysUser
) -> None:
    """业务库里没有这个账号时，名单仍然写入（名单是风控侧的事实）。"""
    result = claimed_case(case_db, auditor, event_id="EVT-1")
    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["blacklist_user"])
    assert outcome.items[0]["exec_result"] == "success"
    assert outcome.items[0]["detail"]["biz_customer_updated"] is False
    assert count(case_db, RcListEntry) == 1


def test_ban_device_writes_device_list(case_db, auditor: SysUser) -> None:
    """ban_device：从案件关联事件的设备号写入设备黑名单。"""
    make_event_row(case_db, event_id="EVT-1", device_id="DEV-X")
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["ban_device"])

    assert outcome.items[0]["exec_result"] == "success"
    assert outcome.items[0]["target_id"] == "DEV-X"
    entry = case_db.execute(select(RcListEntry)).scalar_one()
    assert (entry.list_type, entry.dimension, entry.value) == (LIST_BLACK, DIM_DEVICE, "DEV-X")


def test_ban_device_skips_when_no_device(case_db, auditor: SysUser) -> None:
    """事件没有设备号时封设备是"没有对象可执行"，记 skipped。"""
    make_event_row(case_db, event_id="EVT-1", device_id=None)
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["ban_device"])
    assert outcome.items[0]["exec_result"] == "skipped"
    assert count(case_db, RcListEntry) == 0


def test_watchlist_add_writes_gray_list(case_db, auditor: SysUser) -> None:
    """watchlist_add：写灰名单继续观察，不动业务用户状态。"""
    case_db.add(BizCustomer(user_id="U1", phone="13800000001", status=CUSTOMER_NORMAL))
    case_db.commit()
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    dispose(
        case_db,
        result.case_no,
        auditor,
        business_result="approve",
        risk_actions=["watchlist_add"],
    )

    entry = case_db.execute(select(RcListEntry)).scalar_one()
    assert (entry.list_type, entry.dimension) == (LIST_GRAY, DIM_USER)
    customer = case_db.execute(select(BizCustomer)).scalar_one()
    assert customer.status == CUSTOMER_NORMAL


def test_reject_refund_on_after_sale_scene(case_db, auditor: SysUser) -> None:
    """reject + 售后场景 -> 退款单被驳回（业务结论驱动的联动）。"""
    make_event_row(case_db, event_id="EVT-1", event_type="after_sale_apply", biz_no="REF-1")
    case_db.add(
        BizRefund(
            refund_no="REF-1",
            order_no="ORD-1",
            user_id="U1",
            refund_amount=199.0,
            status=REFUND_APPLIED,
        )
    )
    case_db.commit()
    result = claimed_case(
        case_db, auditor, scene="after_sale", event_type="after_sale_apply", biz_no="REF-1"
    )

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["pass"])

    refund = case_db.execute(select(BizRefund)).scalar_one()
    assert refund.status == REFUND_REJECTED
    # 人工勾选项在前、系统自动联动在后
    assert [item["risk_action"] for item in outcome.items] == ["pass", "reject_refund"]
    assert outcome.items[1]["exec_result"] == "success"


def test_approve_on_after_sale_does_not_touch_refund(case_db, auditor: SysUser) -> None:
    """approve + 售后场景 -> 不动退款单（放行就是放行）。"""
    make_event_row(case_db, event_id="EVT-1", event_type="after_sale_apply", biz_no="REF-2")
    case_db.add(
        BizRefund(
            refund_no="REF-2",
            order_no="ORD-2",
            user_id="U1",
            refund_amount=99.0,
            status=REFUND_APPLIED,
        )
    )
    case_db.commit()
    result = claimed_case(
        case_db, auditor, scene="after_sale", event_type="after_sale_apply", biz_no="REF-2"
    )

    dispose(
        case_db, result.case_no, auditor, business_result="approve", risk_actions=["pass"]
    )
    refund = case_db.execute(select(BizRefund)).scalar_one()
    assert refund.status == REFUND_APPLIED


def test_multiple_actions_execute_all(case_db, auditor: SysUser) -> None:
    """多动作组合：拦订单 + 拉黑 + 封设备 + 灰名单，四项各自落库。"""
    make_event_row(case_db, event_id="EVT-1", event_type="order_create", biz_no="ORD-9", device_id="DEV-9")
    case_db.add(
        BizOrder(
            order_no="ORD-9",
            user_id="U1",
            product_id="P1",
            quantity=1,
            amount=50.0,
            status=ORDER_CREATED,
        )
    )
    case_db.add(BizCustomer(user_id="U1", phone="13800000001", status=CUSTOMER_NORMAL))
    case_db.commit()
    result = claimed_case(
        case_db, auditor, scene="order", event_type="order_create", biz_no="ORD-9"
    )

    outcome = dispose(
        case_db,
        result.case_no,
        auditor,
        risk_actions=["block_order", "blacklist_user", "ban_device", "watchlist_add"],
    )

    assert [item["risk_action"] for item in outcome.items] == [
        "block_order",
        "blacklist_user",
        "ban_device",
        "watchlist_add",
    ]
    assert all(item["exec_result"] == "success" for item in outcome.items)
    assert count(case_db, RcListEntry) == 3
    assert count(case_db, RcCaseActionItem) == 4
    assert case_db.execute(select(BizOrder)).scalar_one().status == ORDER_CANCELLED
    assert case_db.execute(select(BizCustomer)).scalar_one().status == CUSTOMER_BLACKLISTED


def test_repeated_list_write_is_idempotent(case_db, auditor: SysUser) -> None:
    """名单已存在时重复写入不报错、不产生重复条目（INSERT IGNORE 的语义）。

    对应场景：同主体的第二个案件（窗口外新建）再次勾选拉黑。
    """
    case_db.add(RcListEntry(list_type=LIST_BLACK, dimension=DIM_USER, value="U1"))
    case_db.commit()
    result = claimed_case(case_db, auditor, event_id="EVT-1")

    outcome = dispose(case_db, result.case_no, auditor, risk_actions=["blacklist_user"])
    assert outcome.items[0]["exec_result"] == "success"
    assert count(case_db, RcListEntry) == 1
