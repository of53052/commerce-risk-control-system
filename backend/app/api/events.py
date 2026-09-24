"""事件接入接口：``POST /api/v1/events`` 与 ``/events/batch``。

鉴权走 ``X-API-Key``（业务侧通道），与运营侧的 JWT 完全分离 ——
见 app/core/deps.py 的说明。

router 只做三件事：调 service、把结果装进统一响应、把领域异常交给全局处理器。
真正的决策逻辑全在 ``app/services/event_gateway.py``，
因此"重放一个事件"用脚本直接调 service 即可，不必起 HTTP 服务。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.response import success
from app.core.deps import BizCaller, require_biz_key
from app.core.logging import log_kv
from app.db.session import get_db
from app.schemas.event import EventBatchIn, EventIn
from app.services import event_gateway

logger = logging.getLogger("app.api.events")

router = APIRouter(prefix="/api/v1/events", tags=["事件接入"])


@router.post("", summary="单条事件接入（实时决策）")
def create_event(
    payload: EventIn,
    db: Session = Depends(get_db),
    caller: BizCaller = Depends(require_biz_key),
) -> dict:
    """接收一条业务事件，同步返回决策结果。

    重复投递（相同 ``event_id``）不报错，直接回放首次决策并置 ``duplicated=true``：
    业务方重试是常态，"重试拿到 409" 会迫使每个调用方自己维护结果缓存，
    把幂等责任推给上游。这里由风控侧兜住，调用方可以放心重试。
    """
    result = event_gateway.handle_event(db, event=payload)
    log_kv(
        logger,
        logging.INFO,
        "事件接入完成",
        caller=caller.name,
        event_id=result.event_id,
        decision_id=result.decision_id,
        duplicated=result.duplicated,
        cost_ms=result.cost_ms,
    )
    return success(
        {
            "accepted": True,
            "duplicated": result.duplicated,
            "decision": result.response,
        }
    )


@router.post("/batch", summary="批量事件接入（逐条独立事务）")
def create_events(
    payload: EventBatchIn,
    db: Session = Depends(get_db),
    caller: BizCaller = Depends(require_biz_key),
) -> dict:
    """批量接入，返回逐条结果与逐条失败原因。

    响应 HTTP 状态恒为 200（只要请求结构合法）：批量接口的语义是"这批数据我收到了"，
    部分失败通过 ``errors`` 表达。若用 4xx/5xx 表达部分失败，调用方无法区分
    "整批没进来"与"进来了 199 条"，只能整体重试，进而制造重复投递。
    """
    outcome = event_gateway.handle_events(db, events=payload.events)
    log_kv(
        logger,
        logging.INFO,
        "批量事件接入完成",
        caller=caller.name,
        total=len(payload.events),
        accepted=len(outcome.results),
        failed=len(outcome.errors),
    )
    return success(
        {
            "total": len(payload.events),
            "accepted": len(outcome.results),
            "duplicated": sum(1 for item in outcome.results if item.duplicated),
            "decisions": [item.response for item in outcome.results],
            "errors": outcome.errors,
        }
    )
