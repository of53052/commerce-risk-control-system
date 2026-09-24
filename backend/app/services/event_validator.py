"""事件业务校验。

与 Pydantic 的分工（docs/PRD.md §6.3）：

* **结构校验**（``app/schemas/event.py``）：字段在不在、类型对不对 —— 由 FastAPI
  挡在 422，根本没进到本模块；
* **业务校验**（本模块）：同一份事件在不同事件类型下**语义上是否自洽** ——
  ``order_pay`` 引用的订单是否存在、订单是否属于该用户、订单当前状态能不能支付。
  这些必须查库，且失败时要给出"哪个字段错了 + 为什么"，前端才能定位问题。

**为什么要严格校验订单号与退款单号**：``biz_order`` / ``biz_refund`` 是处置联动的
落点（PRD §9.4）。若放任"订单号写错"的事件入库，审核员点"取消订单"时才会发现
订单根本不存在 —— 把问题从接入期推迟到处置期，是可靠性设计里最糟的一类延迟。

时间偏差的处理刻意**不拒绝**：PRD §6.3 规定偏差超阈值只记告警字段。
理由是本系统的输入包含"历史回放"与"仿真"，拒绝未来时间会让回放脚本无法工作；
而未来时间对风控的实际危害有限（最多让窗口特征少算几条），记告警比拒绝更划算。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.biz import ORDER_CANCELLED, ORDER_PAID, BizOrder, BizRefund
from app.models.event import EVENT_ORDER_PAY
from app.services.errors import ErrorCode, NotFoundError, ValidationError
from app.services.messages import ValidationMessage

logger = logging.getLogger("app.services.event_validator")

# 事件时间偏差阈值（秒）：超过则记告警。与 PRD §6.3 的"默认 5 分钟"一致。
CLOCK_SKEW_TOLERANCE_SECONDS = 300

# 各事件类型的必填 payload 字段（类型 -> 字段清单）
REQUIRED_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "login": ("login_type", "login_result"),
    "coupon_receive": ("coupon_id", "face_value"),
    "order_create": ("order_no", "amount"),
    "order_pay": ("order_no", "pay_amount"),
    "after_sale_apply": ("refund_no", "order_no", "refund_amount"),
}


@dataclass
class ValidationOutcome:
    """校验结果。

    ``warnings`` 只提示不拒绝：它们会落进决策响应的 ``warnings`` 与
    ``rc_event.payload`` 的旁路字段，供审核员判断"这条事件的数据质量"。
    """

    warnings: list[str] = field(default_factory=list)
    order: BizOrder | None = None
    refund: BizRefund | None = None


def validate(db: Session, *, event: Any) -> ValidationOutcome:
    """执行全部业务校验，失败时抛 ``BusinessError``。

    ``event`` 是 ``schemas.event.EventIn``（本模块不 import 它，避免
    schemas → services → schemas 的环状依赖，只按属性访问）。
    """
    outcome = ValidationOutcome()
    payload = event.payload or {}

    _check_payload_fields(event_type=event.event_type, payload=payload)
    _check_event_time(event=event, outcome=outcome)

    if event.event_type == EVENT_ORDER_PAY:
        outcome.order = _check_payable_order(db, event=event, payload=payload)

    return outcome


def _check_payload_fields(*, event_type: str, payload: dict[str, Any]) -> None:
    """必填 payload 字段缺失即拒绝（事件类型专有字段见 PRD §6.1）。"""
    required = REQUIRED_PAYLOAD_FIELDS.get(event_type, ())
    missing = [name for name in required if payload.get(name) in (None, "")]
    if missing:
        raise ValidationError(
            f"事件类型 {event_type} 缺少必填字段：{', '.join(missing)}",
            code=ErrorCode.EVENT_PAYLOAD_INVALID,
            field=f"payload.{missing[0]}",
            detail={"missing": missing, "required": list(required)},
        )


def _check_event_time(*, event: Any, outcome: ValidationOutcome) -> None:
    """时间偏差检查：只记告警，不拒绝（理由见模块文档）。"""
    delta = (event.occurred_at - utcnow()).total_seconds()
    if delta > CLOCK_SKEW_TOLERANCE_SECONDS:
        outcome.warnings.append(
            f"事件时间比服务器时间快 {int(delta)} 秒，超出容忍阈值 "
            f"{CLOCK_SKEW_TOLERANCE_SECONDS} 秒，请检查采集端时钟同步"
        )
        logger.warning(
            "事件时间偏差告警",
            extra={"extra_fields": {"event_id": event.event_id, "skew_seconds": int(delta)}},
        )


def _check_payable_order(db: Session, *, event: Any, payload: dict[str, Any]) -> BizOrder:
    """支付事件的订单校验：存在、归属一致、状态可支付。"""
    order_no = str(payload.get("order_no"))
    order = db.execute(select(BizOrder).where(BizOrder.order_no == order_no)).scalar_one_or_none()
    if order is None:
        raise NotFoundError(
            ValidationMessage.ORDER_NOT_FOUND,
            code=ErrorCode.EVENT_ORDER_NOT_FOUND,
            field="payload.order_no",
            detail={"order_no": order_no},
        )
    if order.user_id != event.user_id:
        raise ValidationError(
            ValidationMessage.ORDER_USER_MISMATCH,
            code=ErrorCode.EVENT_PAYLOAD_INVALID,
            field="payload.order_no",
            detail={"order_no": order_no, "order_user_id": order.user_id, "event_user_id": event.user_id},
        )
    if order.status in {ORDER_PAID, ORDER_CANCELLED}:
        raise ValidationError(
            ValidationMessage.PAY_TARGET_NOT_PAID_READY,
            code=ErrorCode.STATE_CONFLICT,
            field="payload.order_no",
            detail={"order_no": order_no, "status": order.status},
        )
    return order
