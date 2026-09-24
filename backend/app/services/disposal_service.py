"""处置服务：双维度结论 + 五类风控动作的执行与联动（docs/PRD.md §9.3/§9.4）。

## 一次提交的完整流程

1. 校验（业务结论合法 / 风控动作非空 / pass 不与其他动作混选 / 备注 ≥ 10 字）；
2. 读案件并校验权限与状态（当前处理人才能处置，admin 例外）；
3. **条件更新** ``processing -> disposed``，影响行数 0 即并发冲突；
4. 执行风控动作（逐项、每项独立记入 ``rc_case_action_item``）；
5. 同事务写 ``rc_case_action`` + 审计，前后状态快照入审计。

## 几个决定的可读化

* **处置必须先接手**：一次提交里读完案件就校验 ``status == processing``。
  pending 案件"直接处置"相当于跳过了"我看过证据"这一步，而处置联动
  改的是真实的业务单据（取消订单/拉黑账号）。

* **admin 可处置任何人的案件（含未接手的 pending）**：权限矩阵里 admin 涵盖一切，
  且它在审计里的 ``actor_name`` 与一般审核员可区分；如果限制 admin 只能处置
  自己接手的案件，答辩演示里"管理员清理现场"会被卡在状态机上。

* **联动失败不阻塞提交**（docs/PRD.md §9.4 明文）：改业务单据失败（订单已取消/
  退款单不存在）时，**不**回滚整个处置。案件处置的核心是"人已经做过了研判"，
  联动是落到业务系统的执行动作；两者绑死会让"订单已被客服先一步取消"这种
  无害冲突把整个处置打成失败，而审核员还是要重新处置一遍同一份证据。
  失败写入 ``rc_case_action_item.exec_result='failed'`` 并带原因，界面逐项展示。

* **``block_order`` / 售后驳回只处理已被关联的同一主体单据**：处置的目标是
  "这次案件的证据链上的单据"，不是主体名下的所有单据。按主体全量取消会
  把"同一账号的正常订单"也取消，业务上属于事故。

* **同一名单 / 单据的重复写入是尽力去重**：名单用 ``INSERT IGNORE`` 靠唯一键兜底；
  订单/退款用条件更新。多次处置同一案件（重试或并发）不会造出重复记录。

* **列表缓存的失效集中在名单写入后**：名单查询走 ``list_service``，
  处置新增的黑/灰名单只有在缓存失效后才会参与下一次决策；漏掉 ``invalidate_cache``
  时新名单要等 60 秒 TTL 才生效，演示里会表现为"明明封了设备却还在放行"。

* **sys_user（审核员）与 biz_customer（业务用户）是两本账**：``blacklist_user``
  改的是 ``biz_customer.status``，不碰 ``sys_user`` —— 处置对象是模拟电商业务
  的用户，不是风控工作台的登录账号。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.biz import (
    CUSTOMER_BLACKLISTED,
    ORDER_CANCELLED,
    ORDER_CREATED,
    BizCustomer,
    BizOrder,
    BizRefund,
)
from app.models.biz import REFUND_REJECTED
from app.models.case import (
    ALL_BIZ_RESULTS,
    ALL_RISK_ACTIONS,
    BIZ_APPROVE,
    BIZ_REJECT,
    CASE_DISPOSED,
    CASE_PROCESSING,
    EXEC_FAILED,
    EXEC_SKIPPED,
    EXEC_SUCCESS,
    EXCLUSIVE_RISK_ACTION,
    MIN_REMARK_LENGTH,
    RISK_BAN_DEVICE,
    RISK_BLACKLIST_USER,
    RISK_BLOCK_ORDER,
    RISK_PASS,
    RISK_WATCHLIST_ADD,
    RcCaseAction,
    RcCaseActionItem,
)
from app.models.rclist import (
    DIM_DEVICE,
    DIM_USER,
    LIST_BLACK,
    LIST_GRAY,
)
from app.services import audit_service, list_service
from app.services.case_service import Actor, get_case, list_case_events
from app.services.errors import (
    ConflictError,
    ErrorCode,
    PermissionError_,
    ValidationError,
)

logger = logging.getLogger("app.services.disposal_service")


@dataclass
class DisposalOutcome:
    """处置提交的结果（每项动作的成败逐项返回）。"""

    case_no: str
    status: str = CASE_DISPOSED
    business_result: str = BIZ_APPROVE
    risk_actions: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    action_id: int = 0


def _normalize_actions(risk_actions: list[str]) -> list[str]:
    """去重并保序（保持提交时的展示顺序）。

    前端把勾选顺序当作"执行顺序"，处置详情按这个顺序回显；
    若这里按 set 去重再排序，回显的顺序就会和提交时看到的不同。
    """
    seen: set[str] = set()
    result: list[str] = []
    for action in risk_actions:
        if action not in seen:
            seen.add(action)
            result.append(action)
    return result


def dispose(
    db: Session,
    *,
    case_no: str,
    actor: Actor,
    business_result: str,
    risk_actions: list[str],
    remark: str,
    now: datetime | None = None,
    commit: bool = True,
) -> DisposalOutcome:
    """提交一次处置。

    参数校验的顺序刻意是"先结构、后状态"：
    结构错误（``business_result`` 拼错、备注太短）在载入案件之前就能拒绝，
    不必为明显的请求错误付一次 SELECT 的代价；而状态冲突（已被别人处置）
    必须读案件才能判定。
    """
    now = now or utcnow()
    remark = remark.strip()
    if not remark:
        raise ValidationError("处置原因必填", code=ErrorCode.PARAM_INVALID, field="remark")
    if len(remark) < MIN_REMARK_LENGTH:
        raise ValidationError(
            f"处置原因至少 {MIN_REMARK_LENGTH} 字（当前 {len(remark)} 字）",
            code=ErrorCode.PARAM_INVALID,
            field="remark",
        )
    if business_result not in ALL_BIZ_RESULTS:
        raise ValidationError(
            f"业务结论不合法：{business_result}（允许 {ALL_BIZ_RESULTS}）",
            code=ErrorCode.PARAM_INVALID,
            field="business_result",
        )
    if not risk_actions:
        raise ValidationError(
            "风控动作至少选择一项", code=ErrorCode.PARAM_INVALID, field="risk_actions"
        )
    unknown = [action for action in risk_actions if action not in ALL_RISK_ACTIONS]
    if unknown:
        raise ValidationError(
            f"未知风控动作：{unknown}", code=ErrorCode.PARAM_INVALID, field="risk_actions"
        )
    if len(risk_actions) > 1 and EXCLUSIVE_RISK_ACTION in risk_actions:
        raise ValidationError(
            f"`{EXCLUSIVE_RISK_ACTION}`（不追加措施）不能与其他动作同时选择",
            code=ErrorCode.PARAM_INVALID,
            field="risk_actions",
        )
    # pass 是"不追加措施"，单选其余动作时由案件本身的状态表达结论；
    # 这里把拒绝对象收窄到"混选"，允许"只选 pass"（等于"通过、不追加"）。
    risk_actions = _normalize_actions(risk_actions)

    case = get_case(db, case_no)

    # 前置校验的**顺序是状态先于权限**，这一点踩过坑：
    # pending 案件的 handler_id 为空（还没人接手），若先判"非当前处理人"，
    # 一个没接手的审核员会收到 40302"只有当前处理人才能处置"——
    # 而他真正的问题是"这个案件还没被接手"。错误码指错方向，前端只能显示
    # 一句与操作无关的提示。先判状态，pending 才会给出"请先接手"。
    #
    # 这些判断只用于给出准确提示，**真正的并发控制是下面的 rowcount**
    # （读到的旧状态不能作为迁移依据）。
    if case.status == "closed":
        raise ConflictError(
            "案件已被管理员强制关闭，无法处置",
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": case.status},
        )
    if case.status == "archived":
        raise ConflictError(
            "案件已归档，无法处置",
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": case.status},
        )
    if case.status == CASE_DISPOSED:
        raise ConflictError(
            "案件已处置，不能重复提交（如需改判请联系管理员）",
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": case.status},
        )
    if case.status == "pending" and actor.role != "admin":
        raise ConflictError(
            "案件尚未接手，请先接手后再提交处置",
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": case.status},
        )
    # 权限：处理中案件只有当前 handler 能处置；admin 是兜底例外（见模块文档）。
    if (
        case.status == CASE_PROCESSING
        and actor.role != "admin"
        and case.handler_id != actor.id
    ):
        raise PermissionError_(
            "只有当前处理人才能处置该案件",
            code=ErrorCode.CASE_HANDLER_MISMATCH,
            detail={"handler": case.handler, "current": actor.name},
        )
    # admin 允许直接从 pending 处置（文档上的"admin 可处置任何人的案件"），
    # 其他角色必须先接手到 processing。
    allowed_before = (CASE_PROCESSING,) if actor.role != "admin" else ("pending", CASE_PROCESSING)

    # 条件更新：这一步**才是**状态迁移的权威判定。前面的 status 判断只用来
    # 给冲突做友好提示；并发下读到的旧状态不能用作迁移依据。
    status_before = case.status
    affected = db.execute(
        update(type(case))
        .where(type(case).case_no == case_no, type(case).status.in_(allowed_before))
        .values(
            status=CASE_DISPOSED,
            disposed_at=now,
            handler=actor.name,
            handler_id=actor.id,
            dispose_result=business_result,
        )
    ).rowcount
    if affected == 0:
        after = get_case(db, case_no)
        raise ConflictError(
            f"案件当前状态为 {after.status}，无法提交处置",
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": after.status, "handler": after.handler},
        )

    # 执行动作后按提交顺序回写明细，action_id 由父行 flush 后回填。
    action_row = RcCaseAction(
        case_no=case_no,
        business_result=business_result,
        risk_actions=risk_actions,
        remark=remark,
        operator_id=actor.id,
        operator_name=actor.name,
        actor_role=actor.role,
        status_before=status_before,
        status_after=CASE_DISPOSED,
    )
    db.add(action_row)
    db.flush()  # 回填 action_id，明细行的外键

    item_rows = _execute_actions(
        db,
        case=case,
        action_row=action_row,
        risk_actions=risk_actions,
        business_result=business_result,
        actor=actor,
    )
    items = [item.to_item_dict() for item in item_rows]

    audit_service.write(
        db,
        action="case_dispose",
        actor_id=str(actor.id) if actor.id is not None else actor.name,
        actor_name=actor.name,
        role=actor.role,
        target_type="case",
        target_id=case_no,
        before={"status": status_before, "handler": case.handler},
        after={
            "status": CASE_DISPOSED,
            "business_result": business_result,
            "risk_actions": risk_actions,
        },
        reason=remark,
    )

    # 置黑 / 置灰之后再失效缓存：若先失效再写库，失效动作会把刚写入的名单
    # 重新以"旧缓存"的形式覆盖（写库还没生效的读请求会缓存旧值），出现
    # "明明写进去了却没生效"的瞬时不一致。顺序反一下，写入即可看见。
    if any(item.risk_action in {RISK_BLACKLIST_USER, RISK_BAN_DEVICE, RISK_WATCHLIST_ADD} for item in item_rows):
        list_service.invalidate_cache(dimension=DIM_USER)
        list_service.invalidate_cache(dimension=DIM_DEVICE)

    if commit:
        db.commit()
    return DisposalOutcome(
        case_no=case_no,
        business_result=business_result,
        risk_actions=risk_actions,
        items=items,
        action_id=action_row.id,
    )


# --------------------------------------------------------------------------- #
# 动作执行
# --------------------------------------------------------------------------- #
@dataclass
class _ActionExec:
    """一项动作的执行结果（内部表示，最终转成 rc_case_action_item）。"""

    action_id: int
    case_no: str
    risk_action: str
    exec_result: str
    target_type: str | None = None
    target_id: str | None = None
    detail: dict | None = None

    def to_item_dict(self) -> dict[str, Any]:
        return {
            "risk_action": self.risk_action,
            "exec_result": self.exec_result,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
        }


def _execute_actions(
    db: Session,
    *,
    case,
    action_row: RcCaseAction,
    risk_actions: list[str],
    business_result: str,
    actor: Actor,
) -> list[_ActionExec]:
    """按提交顺序执行动作，收集每项的执行结果。

    顺序不动：先 ``block_order`` 再 ``blacklist_user``，与前端勾选顺序一致。
    """
    results: list[_ActionExec] = []
    for action in risk_actions:
        if action == RISK_PASS:
            results.append(
                _ActionExec(
                    action_id=action_row.id,
                    case_no=action_row.case_no,
                    risk_action=action,
                    exec_result=EXEC_SUCCESS,
                    detail={"note": "通过，不追加风控措施"},
                )
            )
        elif action == RISK_BLOCK_ORDER:
            results.append(_block_order(db, case=case, action_row=action_row))
        elif action == RISK_BLACKLIST_USER:
            results.append(_blacklist_user(db, case=case, action_row=action_row, actor=actor))
        elif action == RISK_BAN_DEVICE:
            results.append(_ban_device(db, case=case, action_row=action_row, actor=actor))
        elif action == RISK_WATCHLIST_ADD:
            results.append(_watchlist_add(db, case=case, action_row=action_row, actor=actor))

    # 售后场景的复核结论是另一个联动面（不挂在 risk_actions 上）：
    # PRD §9.4 的"reject + 售后场景"是业务结论驱动的，不是"再勾选一个动作"。
    # 它排在人工勾选项之后，处置详情里因此先列"我做了什么"、再列"系统自动做了什么"。
    if case.scene == "after_sale" and business_result == BIZ_REJECT:
        results.append(_reject_refund(db, case=case, action_row=action_row))

    # 明细表刻意**不建唯一键**：同一案件上"拉黑账号 + 封设备 + 拦订单"是常态，
    # 唯一键只会限制将来新增同类动作（比如两次 watchlist_add 指向不同名单）。
    # 因此这里按普通 ORM 路径写入，不需要唯一冲突处理。
    for item in results:
        db.add(
            RcCaseActionItem(
                action_id=item.action_id,
                case_no=item.case_no,
                risk_action=item.risk_action,
                exec_result=item.exec_result,
                target_type=item.target_type,
                target_id=item.target_id,
                detail=item.detail,
            )
        )
    db.flush()
    return results


def _block_order(db: Session, *, case, action_row: RcCaseAction) -> _ActionExec:
    """取消未支付的关联订单（docs/PRD.md §9.4）。

    只处理**尚未支付**的订单：已支付订单的资金已经结算，强行取消属于财务事故；
    对它只能给出"需人工退款"提示。条件更新是为了让"别的客服已经取消"这种
    并发也给出**准确**的 skipped 而不是误报失败。
    """
    order_nos = [
        event.biz_no
        for event in list_case_events(db, action_row.case_no)
        if event.event_type in {"order_create", "order_pay"} and event.biz_no
    ]
    # 关联事件里没有订单号：案件主体是"只登录或只领券的账号"，
    # 此时 block_order 是"没有对象可执行"，用 skipped 而不是 failed 更准确。
    if not order_nos:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action=RISK_BLOCK_ORDER,
            exec_result=EXEC_SKIPPED,
            target_type="order",
            target_id=None,
            detail={"reason": "案件关联事件无订单，无需拦截"},
        )
    order_no = order_nos[0]

    affected = db.execute(
        update(BizOrder)
        .where(BizOrder.order_no == order_no, BizOrder.status == ORDER_CREATED)
        .values(status=ORDER_CANCELLED, updated_at=utcnow())
    ).rowcount

    row = db.execute(select(BizOrder).where(BizOrder.order_no == order_no)).scalar_one_or_none()
    if affected > 0:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action=RISK_BLOCK_ORDER,
            exec_result=EXEC_SUCCESS,
            target_type="order",
            target_id=order_no,
            detail={"from": ORDER_CREATED, "to": ORDER_CANCELLED},
        )
    if row is None:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action=RISK_BLOCK_ORDER,
            exec_result=EXEC_FAILED,
            target_type="order",
            target_id=order_no,
            detail={"reason": f"订单不存在：{order_no}"},
        )
    # 条件更新影响 0 行但订单还在：状态不是 created（已取消/已支付/已退款）。
    return _ActionExec(
        action_id=action_row.id,
        case_no=action_row.case_no,
        risk_action=RISK_BLOCK_ORDER,
        exec_result=EXEC_SKIPPED,
        target_type="order",
        target_id=order_no,
        detail={"reason": f"订单当前状态为 {row.status}，仅未支付订单可拦截", "status": row.status},
    )


def _reject_refund(db: Session, *, case, action_row: RcCaseAction) -> _ActionExec:
    """售后场景下驳回退款单（仅 applied 状态可驳回）。

    挂在 ``risk_action="reject_refund"`` 是为了与 risk_actions 列表区分：
    处置项明细里审核员希望"我勾了什么 + 系统自动做了什么"分两列展示，
    否则"驳回退款"看起来像是人工勾选的，而它其实由业务结论驱动。
    """
    refund_nos = [
        event.biz_no
        for event in list_case_events(db, action_row.case_no)
        if event.event_type == "after_sale_apply" and event.biz_no
    ]
    if not refund_nos:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action="reject_refund",
            exec_result=EXEC_SKIPPED,
            target_type="refund",
            target_id=None,
            detail={"reason": "案件关联事件无退款单，无需驳回"},
        )
    refund_no = refund_nos[0]

    affected = db.execute(
        update(BizRefund)
        .where(BizRefund.refund_no == refund_no, BizRefund.status == "applied")
        .values(status=REFUND_REJECTED)
    ).rowcount
    row = db.execute(select(BizRefund).where(BizRefund.refund_no == refund_no)).scalar_one_or_none()
    if affected > 0:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action="reject_refund",
            exec_result=EXEC_SUCCESS,
            target_type="refund",
            target_id=refund_no,
            detail={"from": "applied", "to": REFUND_REJECTED},
        )
    if row is None:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action="reject_refund",
            exec_result=EXEC_FAILED,
            target_type="refund",
            target_id=refund_no,
            detail={"reason": f"退款单不存在：{refund_no}"},
        )
    return _ActionExec(
        action_id=action_row.id,
        case_no=action_row.case_no,
        risk_action="reject_refund",
        exec_result=EXEC_SKIPPED,
        target_type="refund",
        target_id=refund_no,
        detail={"reason": f"退款单当前状态为 {row.status}，仅待审核可驳回", "status": row.status},
    )


def _blacklist_user(
    db: Session, *, case, action_row: RcCaseAction, actor: Actor
) -> _ActionExec:
    """拉黑账号（名单 + 业务用户置黑）。

    两步在同一事务里：只在名单里拉黑但不改 ``biz_customer.status``，业务端
    仍会认为这是"正常用户"；只改业务用户不写名单，下一轮决策又会因为
    名单缺失而放行。
    """
    _insert_list_entry(
        db,
        list_type=LIST_BLACK,
        dimension=DIM_USER,
        value=case.subject_value,
        reason=f"案件 {case.case_no} 处置拉黑",
        created_by=actor.name,
    )
    biz_updated = (
        db.execute(
            update(BizCustomer)
            .where(BizCustomer.user_id == case.subject_value)
            .values(status=CUSTOMER_BLACKLISTED)
        ).rowcount
        or 0
    )
    return _ActionExec(
        action_id=action_row.id,
        case_no=action_row.case_no,
        risk_action=RISK_BLACKLIST_USER,
        exec_result=EXEC_SUCCESS,
        target_type="customer",
        target_id=case.subject_value,
        detail={
            "list_added": True,
            "biz_customer_updated": bool(biz_updated),
        },
    )


def _ban_device(db: Session, *, case, action_row: RcCaseAction, actor: Actor) -> _ActionExec:
    """封禁设备（仅写名单；设备没有业务用户侧的置黑动作）。

    设备 ID 从案件关联事件里取：同一用户可能在多台设备上作案，
    封哪台的判断依据是"这次案件的证据链里出现过哪台"。
    """
    device_id = _device_id_of_case(db, action_row.case_no)
    if not device_id:
        return _ActionExec(
            action_id=action_row.id,
            case_no=action_row.case_no,
            risk_action=RISK_BAN_DEVICE,
            exec_result=EXEC_SKIPPED,
            target_type="list_entry",
            target_id=None,
            detail={"reason": "案件关联事件无设备 ID，无法封禁"},
        )
    _insert_list_entry(
        db,
        list_type=LIST_BLACK,
        dimension=DIM_DEVICE,
        value=device_id,
        reason=f"案件 {case.case_no} 处置封禁设备",
        created_by=actor.name,
    )
    return _ActionExec(
        action_id=action_row.id,
        case_no=action_row.case_no,
        risk_action=RISK_BAN_DEVICE,
        exec_result=EXEC_SUCCESS,
        target_type="list_entry",
        target_id=device_id,
        detail={"dimension": DIM_DEVICE},
    )


def _watchlist_add(
    db: Session, *, case, action_row: RcCaseAction, actor: Actor
) -> _ActionExec:
    """加入灰名单（继续观察，不做业务侧置黑）。

    灰名单的语义是"还不足以定案"：同一主体可能又触发规则又看起来不太像作弊，
    此时拉黑是过度处置。把它与 ``blacklist_user`` 区分开，是与处置结论
    单选 ``approve`` 组合时的常见选择（"这次我认了，但要继续盯"）。
    """
    _insert_list_entry(
        db,
        list_type=LIST_GRAY,
        dimension=DIM_USER,
        value=case.subject_value,
        reason=f"案件 {case.case_no} 加入观察名单",
        created_by=actor.name,
    )
    return _ActionExec(
        action_id=action_row.id,
        case_no=action_row.case_no,
        risk_action=RISK_WATCHLIST_ADD,
        exec_result=EXEC_SUCCESS,
        target_type="list_entry",
        target_id=case.subject_value,
        detail={"dimension": DIM_USER, "list_type": LIST_GRAY},
    )


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _insert_list_entry(
    db: Session,
    *,
    list_type: str,
    dimension: str,
    value: str,
    reason: str,
    created_by: str,
) -> None:
    """尽力写入名单（重复不报错）。

    用 ``INSERT IGNORE`` 而不是"先查后插"：先查后插对重复处置是安全的，
    但并发下两次先查都返回"不存在"，随后两次 insert 一次成功一次撞唯一键，
    撞键的一次会被翻译成 409 让操作者困惑。``INSERT IGNORE`` 让"已存在"与
    "写入成功"得到同一个结果，副作用是**已存在时不会更新 reason**。
    我们接受这个副作用：名单原因是"当时为什么这么写"，不是编辑记录；
    真正要看原因的应该看案件处置记录与审计。
    """
    db.execute(
        text(
            "INSERT IGNORE INTO rc_list_entry "
            "(list_type, dimension, value, priority, reason, source, expire_at, "
            "status, created_by, created_at, updated_at) "
            "VALUES (:list_type, :dimension, :value, :priority, :reason, :source, "
            "NULL, 'active', :created_by, UTC_TIMESTAMP(), UTC_TIMESTAMP())"
        ),
        {
            "list_type": list_type,
            "dimension": dimension,
            "value": value,
            "priority": 100,
            "reason": reason,
            "source": "manual",
            "created_by": created_by,
        },
    )


def _device_id_of_case(db: Session, case_no: str) -> str | None:
    """取案件关联事件里最近一台有设备号的设备。

    案件与事件是 1:N，设备 ID 在事件表里而不是案件表里（案件只能存"建案
    那一刻的设备"，合案带来的新事件会换来新设备；压成一个字段会丢掉证据链
    上的细节）。**一次 IN 查询取最近一条**，而不是逐个事件查一遍 ——
    合案案件可能有几十条事件，逐条查会把一次处置变成几十次往返。
    """
    from app.models.event import RcEvent  # 局部导入：仅在处置路径需要事件域

    event_ids = [item.event_id for item in list_case_events(db, case_no)]
    if not event_ids:
        return None
    return db.execute(
        select(RcEvent.device_id)
        .where(RcEvent.event_id.in_(event_ids), RcEvent.device_id.isnot(None))
        .order_by(RcEvent.occurred_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def item_to_dict(item: RcCaseActionItem) -> dict[str, Any]:
    """处置明细行（含时间），用于处置记录详情。"""
    return {
        "id": item.id,
        "risk_action": item.risk_action,
        "exec_result": item.exec_result,
        "target_type": item.target_type,
        "target_id": item.target_id,
        "detail": item.detail,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }
