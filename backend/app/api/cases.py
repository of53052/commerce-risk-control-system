"""案件接口：列表、详情、接手、处置、归档、强制关闭（docs/PRD.md §12）。

路由层只做三件事：解析/校验入参、调用服务、装进统一响应。
状态机与处置联动全在 ``app/services/case_service.py`` 与
``app/services/disposal_service.py`` 里，因此"接手一个案件"可以在
脚本或测试里直接调用服务函数而无需起 HTTP 服务。

权限（docs/PRD.md §4.2）：

============================  ==============================
接口                          允许角色
============================  ==============================
列表 / 详情                   登录即可（auditor / strategist / admin）
接手 / 处置                   auditor / admin
归档 / 强制关闭               admin
============================  ==============================

``strategist`` 能看案件但不能处置 —— 他需要靠案件数据判断策略效果，
但没有研判职责；权限矩阵这样切，是为了让"谁改的策略"与"谁做的处置"
在审计里永远是两个不同的字段。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.response import page as page_body, success
from app.core.deps import Operator, get_current_operator, require_roles
from app.core.logging import log_kv
from app.db.session import get_db
from app.schemas.case import ArchiveIn, CloseIn, DisposeIn
from app.services import case_service, disposal_service
from app.services.case_service import Actor, CaseQuery, build_case_detail, case_to_dict

logger = logging.getLogger("app.api.cases")

router = APIRouter(prefix="/api/v1/cases", tags=["案件"])

# 列表默认页大小与上限：上限存在的意义是防止"size=100000"把一次列表请求
# 变成全表扫描 + 大响应（演示系统同样会被自己的脚本误伤）。
_DEFAULT_PAGE_SIZE = 20
_MAX_PAGE_SIZE = 100


def _actor(operator: Operator) -> Actor:
    """接口层的 ``Operator`` -> 服务层的 ``Actor``（服务层不依赖 FastAPI）。"""
    return Actor(id=operator.id, name=operator.username, role=operator.role)


@router.get("", summary="案件列表（工作台左栏）")
def list_cases(
    status: str | None = Query(default=None, description="pending/processing/disposed/archived/closed"),
    risk_level: str | None = Query(default=None, description="low/mid/high"),
    scene: str | None = Query(default=None, description="login/coupon/order/after_sale"),
    subject_type: str | None = Query(default=None),
    keyword: str | None = Query(default=None, description="案件号或主体值（模糊匹配）"),
    mine: bool = Query(default=False, description="只看我接手的案件"),
    start_at: datetime | None = Query(default=None, description="末次触发时间起（ISO8601）"),
    end_at: datetime | None = Query(default=None, description="末次触发时间止（ISO8601）"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=_DEFAULT_PAGE_SIZE, ge=1, le=_MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    operator: Operator = Depends(get_current_operator),
) -> dict:
    """案件分页列表，附带各状态计数供前端渲染筛选标签。"""
    query = CaseQuery(
        status=status,
        risk_level=risk_level,
        scene=scene,
        subject_type=subject_type,
        handler_id=operator.id if mine else None,
        keyword=keyword,
        start_at=start_at,
        end_at=end_at,
    )
    rows, total = case_service.list_cases(db, query=query, page_no=page, size=size)
    body = page_body(
        [case_to_dict(row) for row in rows], total=total, page_no=page, size=size
    )
    # 状态计数与列表同一次响应返回：工作台的筛选标签要显示"待审 12 / 处理中 3"，
    # 单独再发一次请求会让首屏多一个来回，而这条聚合走 (status, last_at) 索引。
    body["status_counts"] = case_service.status_counts(db)
    return success(body)


@router.get("/{case_no}", summary="案件详情（画像 / 单据 / 特征 / 图谱 / 证据）")
def get_case_detail(
    case_no: str,
    db: Session = Depends(get_db),
    operator: Operator = Depends(get_current_operator),
) -> dict:
    row = case_service.get_case(db, case_no)
    return success(build_case_detail(db, row))


@router.post("/{case_no}/claim", summary="接手案件")
def claim_case(
    case_no: str,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_roles("auditor", "admin")),
) -> dict:
    """接手：``pending -> processing``。冲突时返回 409 + 当前处理人。"""
    row = case_service.claim(db, case_no=case_no, actor=_actor(operator))
    log_kv(logger, logging.INFO, "案件接手", case_no=case_no, handler=operator.username)
    return success(case_to_dict(row))


@router.post("/{case_no}/dispose", summary="提交处置（双维度结论 + 联动）")
def dispose_case(
    case_no: str,
    payload: DisposeIn,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_roles("auditor", "admin")),
) -> dict:
    """提交处置。

    **联动失败也是 200**：逐项结果放在 ``items`` 里（exec_result=success/failed/skipped）。
    用 5xx 表达"订单已被客服先一步取消"会把一次有效的处置变成错误，
    而审核员的处置结论本身是成功的（docs/PRD.md §9.4）。
    """
    outcome = disposal_service.dispose(
        db,
        case_no=case_no,
        actor=_actor(operator),
        business_result=payload.business_result,
        risk_actions=list(payload.risk_actions),
        remark=payload.remark,
    )
    case_row = case_service.get_case(db, case_no)
    log_kv(
        logger,
        logging.INFO,
        "案件处置完成",
        case_no=case_no,
        operator=operator.username,
        business_result=payload.business_result,
        risk_actions=",".join(payload.risk_actions),
        failed_items=sum(1 for item in outcome.items if item["exec_result"] == "failed"),
    )
    return success(
        {
            "case": case_to_dict(case_row),
            "action_id": outcome.action_id,
            "business_result": outcome.business_result,
            "risk_actions": outcome.risk_actions,
            "items": outcome.items,
        }
    )


@router.post("/archive", summary="批量归档（admin）")
def archive_cases(
    payload: ArchiveIn,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_roles("admin")),
) -> dict:
    """批量归档，逐条返回成败。

    响应恒为 200：批量动作的语义是"这些我都提交了"，部分失败通过 ``results``
    表达（与 ``events/batch`` 的口径一致）。这样前端可以提示
    "成功 9 条，1 条状态不允许"，而不是让操作者整体重试一遍。
    """
    results = case_service.archive(db, case_nos=list(payload.case_nos), actor=_actor(operator))
    log_kv(
        logger,
        logging.INFO,
        "案件批量归档",
        operator=operator.username,
        total=len(results),
        ok=sum(1 for item in results if item["ok"]),
    )
    return success(
        {
            "total": len(results),
            "ok": sum(1 for item in results if item["ok"]),
            "results": results,
        }
    )


@router.post("/{case_no}/archive", summary="归档单个案件（admin）")
def archive_case(
    case_no: str,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_roles("admin")),
) -> dict:
    results = case_service.archive(db, case_nos=[case_no], actor=_actor(operator))
    result = results[0]
    row = case_service.get_case(db, case_no)
    log_kv(logger, logging.INFO, "案件归档", case_no=case_no, operator=operator.username)
    return success({"result": result, "case": case_to_dict(row)})


@router.post("/{case_no}/close", summary="强制关闭（admin，需填原因）")
def close_case(
    case_no: str,
    payload: CloseIn,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_roles("admin")),
) -> dict:
    row = case_service.close(
        db, case_no=case_no, actor=_actor(operator), reason=payload.reason
    )
    log_kv(logger, logging.INFO, "案件强制关闭", case_no=case_no, operator=operator.username)
    return success(case_to_dict(row))
