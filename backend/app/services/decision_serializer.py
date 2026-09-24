"""决策结果的落库与序列化（把「一次决策」变成「一行行数据」和「一份响应」）。

抽成独立模块的理由：

1. 落库要写 5 张表（rc_event / rc_feature_snapshot / rc_decision /
   rc_decision_hit / rc_model_contribution），塞进 event_gateway 会让那个文件
   在"链路编排"之外再多一层"字段搬运"，可读性显著下降；
2. **同一份结果要落两种介质**：MySQL（结构化落点，供查询与统计）与
   Redis（幂等缓存，重复投递时直接回放响应）。两者字段口径必须完全一致，
   放在一个模块里能做到"改一处、两边同步"，避免出现
   "缓存回放的 action 与库里存的不一样"这种最难排查的不一致。

缓存的**权威性**很关键：P0 采用「同步双写、Redis 为主」——
首次决策先写 MySQL（在同一事务里，保证"响应了给业务方就一定落库了"），
成功后写 Redis 幂等缓存。若先写缓存、落库失败，业务方拿到的是一个
库里根本不存在的决策，审核工作台永远查不到它，这类"幽灵决策"比慢一点危险得多。
"""

from __future__ import annotations

import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.models.decision import RcDecision, RcDecisionHit, RcModelContribution
from app.models.event import RcEvent, RcFeatureSnapshot
from app.services.feature_engine import split_context
from app.services.fusion import FusionResult

logger = logging.getLogger("app.services.decision_serializer")

# 落 rc_feature_snapshot / rc_decision 的字段白名单之外的上下文键，
# 由 split_context 自动分离（见 feature_engine.NON_FEATURE_CONTEXT_KEYS 说明）。


def build_decision_row(
    *,
    decision_id: str,
    event_id: str,
    fusion: FusionResult,
    hit_count: int,
    latency_ms: int,
    case_no: str | None = None,
) -> RcDecision:
    """组装 rc_decision 行。"""
    return RcDecision(
        decision_id=decision_id,
        event_id=event_id,
        rule_score=fusion.rule_score,
        model_score=fusion.model_score,
        risk_score=fusion.risk_score,
        risk_level=fusion.risk_level,
        action=fusion.action,
        action_hint=fusion.action_hint,
        decided_by=fusion.decided_by,
        fusion_alpha=fusion.fusion_alpha,
        fusion_mode=fusion.fusion_mode,
        model_version=fusion.model_version,
        case_no=case_no,
        hit_count=hit_count,
        latency_ms=latency_ms,
        from_cache=False,
    )


def build_hit_rows(*, decision_id: str, hits: list[Any]) -> list[RcDecisionHit]:
    """组装 rc_decision_hit 行。``hits`` 是 rule_engine.RuleHit 列表。"""
    return [
        RcDecisionHit(
            decision_id=decision_id,
            rule_code=hit.rule_code,
            rule_name=hit.rule_name,
            rule_category=hit.rule_category,
            score=hit.score,
            reason=hit.reason[:255] if hit.reason else None,
            # 证据链可能很长（每个叶子条件一条），MySQL JSON 列能存，
            # 但超过 64KB 会被截断报错，因此这里限长并留痕在日志里。
            evidence=_trim_evidence(hit.evidence, decision_id=decision_id, rule_code=hit.rule_code),
        )
        for hit in hits
    ]


def build_contribution_rows(*, decision_id: str, contributions: list[dict[str, Any]]) -> list[RcModelContribution]:
    """组装 rc_model_contribution 行（模型引擎已只返回 top-5）。"""
    rows: list[RcModelContribution] = []
    for index, item in enumerate(contributions, start=1):
        rows.append(
            RcModelContribution(
                decision_id=decision_id,
                feature_name=str(item.get("feature_name"))[:64],
                feature_value=_to_float(item.get("feature_value")),
                contribution=_to_float(item.get("contribution")) or 0.0,
                direction=str(item.get("direction") or "up")[:8],
                rank_no=int(item.get("rank_no") or index),
            )
        )
    return rows


def to_response(
    *,
    decision_row: RcDecision,
    event_type: str,
    user_id: str,
    occurred_at: datetime,
    biz_no: str | None,
    features: dict[str, Any],
    context: dict[str, Any],
    missing_fields: list[str],
    hits: list[Any],
    contributions: list[dict[str, Any]],
    list_hits: list[dict[str, Any]],
    notes: list[str],
    warnings: list[str],
    cost_ms: int,
    from_cache: bool = False,
) -> dict[str, Any]:
    """组装对外的决策响应（docs/PRD.md §8.5 的字段分区）。

    ``features`` 与 ``context`` 刻意**分成两个字段**而不是合并成一个扁平字典：

    * ``features`` 是特征快照 —— 参与规则求值与模型推理的键，
      与落库的 rc_feature_snapshot 完全一致；
    * ``context`` 是"当时看到的事实"（事件信封与业务 payload），
      只用于审核员核对证据，**不参与任何打分**。

    合并它们会带来一个隐蔽且危险的后果：前端与仿真页无法区分
    "这个键影响了决策"与"这个键只是被展示了"，审核员复盘时会把
    展示用的原始字段误当成判据。数据口径的清晰比少一层嵌套更重要。
    """
    return {
        "decision_id": decision_row.decision_id,
        "event_id": decision_row.event_id,
        "event_type": event_type,
        "user_id": user_id,
        "biz_no": biz_no,
        "occurred_at": occurred_at.isoformat(),
        "rule_score": decision_row.rule_score,
        "model_score": decision_row.model_score,
        "risk_score": decision_row.risk_score,
        "risk_level": decision_row.risk_level,
        "action": decision_row.action,
        "action_hint": decision_row.action_hint,
        "decided_by": decision_row.decided_by,
        "case_no": decision_row.case_no,
        "model_version": decision_row.model_version,
        "rule_hits": [_hit_to_dict(hit) for hit in hits],
        "features": features,
        "context": context,
        "missing_fields": missing_fields,
        "model_contributions": [_contribution_to_dict(item) for item in contributions],
        "list_hits": list_hits,
        "latency_ms": decision_row.latency_ms,
        "from_cache": from_cache,
        "notes": notes,
        "warnings": warnings,
        "cost_ms": cost_ms,
    }


def snapshot_payload(features: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """拆出 (特征快照, 上下文)。委托给 feature_engine，保证口径唯一。"""
    return split_context(features)


def load_decision_payload(db, event_id: str) -> dict[str, Any] | None:
    """按 event_id 从数据库还原一份决策响应；不存在返回 None。

    为什么需要它：幂等的第一层是 Redis 缓存，但它可能被清空（重启、手动 FLUSHDB、
    TTL 到期）。此时若直接拒绝或重复决策，都不符合 PRD §6.3「重复提交直接返回
    首次决策结果」的约定。从库里还原是唯一正确的兜底 —— 决策本身就完整落库了，
    数据一直都在，只是快路径没了。

    **已知损失**：``list_hits`` / ``notes`` / ``warnings`` 未落库，
    因此还原出来的响应这三项为空。它们是"决策过程的旁注"而非决策本身，
    为此单开三张表不划算。Redis 命中时会话路径不受影响（缓存里存的是完整响应），
    所以这条损失只在"缓存丢失 + 重复投递"这个组合下出现。
    """
    decision = db.execute(
        select(RcDecision).where(RcDecision.event_id == event_id)
    ).scalar_one_or_none()
    if decision is None:
        return None

    event = db.execute(select(RcEvent).where(RcEvent.event_id == event_id)).scalar_one_or_none()
    snapshot = db.execute(
        select(RcFeatureSnapshot).where(RcFeatureSnapshot.event_id == event_id)
    ).scalar_one_or_none()
    hits = (
        db.execute(
            select(RcDecisionHit)
            .where(RcDecisionHit.decision_id == decision.decision_id)
            .order_by(RcDecisionHit.id.asc())
        )
        .scalars()
        .all()
    )
    contributions = (
        db.execute(
            select(RcModelContribution)
            .where(RcModelContribution.decision_id == decision.decision_id)
            .order_by(RcModelContribution.rank_no.asc())
        )
        .scalars()
        .all()
    )

    return {
        "decision_id": decision.decision_id,
        "event_id": decision.event_id,
        "event_type": event.event_type if event else None,
        "user_id": event.user_id if event else None,
        "biz_no": event.biz_no if event else None,
        "occurred_at": event.occurred_at.isoformat() if event else None,
        "rule_score": decision.rule_score,
        "model_score": decision.model_score,
        "risk_score": decision.risk_score,
        "risk_level": decision.risk_level,
        "action": decision.action,
        "action_hint": decision.action_hint,
        "decided_by": decision.decided_by,
        "case_no": decision.case_no,
        "model_version": decision.model_version,
        "rule_hits": [
            {
                "rule_code": row.rule_code,
                "rule_name": row.rule_name,
                "rule_category": row.rule_category,
                "score": row.score,
                "reason": row.reason,
                "evidence": row.evidence or [],
                "missing_fields": [],
                "errors": [],
            }
            for row in hits
        ],
        "features": dict(snapshot.features) if snapshot else {},
        "context": _event_context(event),
        "missing_fields": [],
        "model_contributions": [
            {
                "feature_name": row.feature_name,
                "feature_value": float(row.feature_value) if row.feature_value is not None else None,
                "contribution": float(row.contribution),
                "direction": row.direction,
                "rank_no": row.rank_no,
            }
            for row in contributions
        ],
        "list_hits": [],
        "latency_ms": decision.latency_ms,
        "from_cache": True,
        "notes": ["本次响应由数据库中的首次决策还原（幂等缓存已失效）"],
        "warnings": [],
        "cost_ms": snapshot.calc_cost_ms if snapshot else None,
        "source": event.source if event else None,
    }


def _event_context(event: RcEvent | None) -> dict[str, Any]:
    """从 rc_event 还原展示用上下文（重复投递回放时使用）。

    只还原**落库过的事实**：payload 与信封字段。设备指纹与 IP 归属地都在
    rc_event 里有列，因此这里不会"缺一半"。还原不出来时返回空 dict 而不是
    编造占位内容 —— 前端据此显示"上下文不可用"比显示假数据安全。
    """
    if event is None:
        return {}
    return {
        "payload": dict(event.payload or {}),
        "event": {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "user_id": event.user_id,
            "device_id": event.device_id,
            "device_fingerprint": dict(event.device_fingerprint or {}),
            "ip": event.ip,
            "ip_region": event.ip_region,
            "address_hash": event.address_hash,
            "biz_no": event.biz_no,
            "source": event.source,
        },
    }


def _hit_to_dict(hit: Any) -> dict[str, Any]:
    """RuleHit（dataclass）-> dict。"""
    if is_dataclass(hit):
        data = {key: value for key, value in asdict(hit).items() if key not in {"missing_fields", "errors"}}
        data["missing_fields"] = list(getattr(hit, "missing_fields", []) or [])
        data["errors"] = list(getattr(hit, "errors", []) or [])
        return data
    return dict(hit)


def _contribution_to_dict(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "feature_name": item.get("feature_name"),
        "feature_value": item.get("feature_value"),
        "contribution": item.get("contribution"),
        "direction": item.get("direction"),
        "rank_no": item.get("rank_no"),
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trim_evidence(evidence: list[dict[str, Any]], *, decision_id: str, rule_code: str) -> list[dict[str, Any]]:
    """限制证据链条数：单个条件的证据约占 150 字节，64KB 上限约合 400 条。

    正常规则的叶子条件不会超过几十个，触发截断说明规则写得过于复杂 ——
    这本身是需要被发现的信号，因此截断时记 WARNING 而不是静默处理。
    """
    limit = 200
    if len(evidence) <= limit:
        return evidence
    logger.warning(
        "规则证据链过长，已截断",
        extra={"extra_fields": {"decision_id": decision_id, "rule_code": rule_code, "leaves": len(evidence)}},
    )
    return evidence[:limit]
