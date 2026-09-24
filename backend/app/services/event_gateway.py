"""事件网关：把一次业务动作串成一条决策链，并留下可回放的痕迹。

链路（docs/ARCHITECTURE.md §5.1）::

    业务校验 → 幂等检查 → 名单匹配 → 特征计算 → 规则求值 → 模型推理
             → 融合仲裁 → 落库 → 更新事件条带 → 写幂等缓存

几个刻意为之的顺序决策：

1. **名单直判时仍然算特征**。名单命中就 Reject/Pass，规则与模型都不跑 ——
   但审核员打开案件时要能看到"当时的行为频次是多少"。跳过特征会让
   案件详情只剩一句"命中黑名单"，处置依据不足。代价是黑名单事件多花几毫秒，
   相对于它带来的可解释性，这个交换是划算的。

2. **先落库、后写条带与缓存**。这里刻意**不采用**架构文档 §5.1 的异步落库：
   P0 是单进程演示系统，同步写 5 张表在本地约 5~15ms，仍在 P95<50ms 预算内；
   而一旦有未提交事务被 DDL 撞上，MySQL 会静默等表级元数据锁直到超时（踩过坑），
   异步写入会让这种挂起变得难以定位。**代价已记录**：接入接口的可用性与 MySQL
   耦合，MySQL 不可用时事件接入直接失败（返回 5xx 而不是"先收后补"）。

3. **只有落库成功才写条带与幂等缓存**。否则会出现"特征窗口里有这条事件，
   但库里查不到"的漂移 —— 窗口与库不一致会让后续所有决策的特征值都不对，
   而且几乎无法事后定位。

4. **条带写入用事件自身时间戳**，且必须在决策之后（特征计算时当前事件还未入条带，
   由特征引擎自行把当前事件计入，见 feature_engine._compute_sum 的注释）。

已知 P0 限制：``Review`` / ``Reject`` 应生成案件（PRD §9.1），但案件表属 P1，
因此 P0 只产出决策、``case_no`` 留空，并在此处记 ``notes`` 说明。
"""

from __future__ import annotations

import logging
import itertools
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import orjson
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import decision_id_ctx, event_id_ctx, log_kv
from app.core.timeutil import to_ms
from app.db.redis_client import IDEM_TTL, get_redis, idem_key
from app.models.event import RcEvent, RcFeatureSnapshot
from app.services import (
    decision_serializer,
    event_validator,
    feature_engine,
    fusion,
    list_service,
    model_engine,
    rule_engine,
    window_store,
)
from app.services.config_service import get_float, get_int, get_str
from app.services.errors import BusinessError, ConflictError, ErrorCode
from app.services.messages import DecisionReason, dimension_cn

logger = logging.getLogger("app.services.event_gateway")

# 场景映射：事件类型 -> 规则 scene。与 app/seeds/rules.py 的 scene 取值一致：
# 领券与下单同属「大促薅羊毛」场景，因此共用 coupon 场景规则集。
SCENE_BY_EVENT_TYPE: dict[str, str] = {
    "login": "login",
    "coupon_receive": "coupon",
    "order_create": "order",
    "order_pay": "order",
    "after_sale_apply": "after_sale",
}

# 各事件类型取哪个 payload 字段作为「关联业务单据号」
BIZ_NO_FIELD: dict[str, str] = {
    "coupon_receive": "coupon_id",
    "order_create": "order_no",
    "order_pay": "order_no",
    "after_sale_apply": "refund_no",
}

# 各事件类型在条带里携带的金额（用于窗口求和特征）
AMOUNT_FIELD: dict[str, str] = {
    "coupon_receive": "face_value",
    "order_create": "amount",
    "order_pay": "pay_amount",
    "after_sale_apply": "refund_amount",
}

# 业务号（决策号/案件号）的唯一性来源：时间戳 + 进程盐 + 进程内自增序号。
#
# 时间戳只精确到秒：``app.core.timeutil.utcnow()`` 刻意截掉了微秒
# （审计哈希链按秒归一，见 audit_service），因此 ``%f`` 恒为 000000，
# 指望它提供唯一性是错觉。
#
# 曾经的实现是"D + 秒级时间戳 + 3 位随机数"，在**同一秒内超过约 40 条决策**时
# 就会开始撞唯一索引（生日悖论：1000 个取值、140 条记录，撞车概率已接近 1），
# 而撞车的表现极具误导性 —— 网关把 IntegrityError 一律翻译成
# "事件编号已存在（并发重复投递）"，于是调用方被告知"换个 event_id 重试"，
# 而真正重复的是决策号：换个 event_id 重试照样失败。模拟端的批量场景
# （一秒内近百条事件）立刻把这个设计缺陷打了出来。
_ID_TIME_FORMAT = "%Y%m%d%H%M%S"
_ID_PROCESS_SALT = secrets.token_hex(3).upper()
_ID_COUNTER = itertools.count(1)


@dataclass
class DecisionResult:
    """一次决策的完整产物（供接口层组装响应）。"""

    decision_id: str
    event_id: str
    accepted: bool
    duplicated: bool
    response: dict[str, Any]
    case_no: str | None = None
    cost_ms: int = 0
    notes: list[str] = field(default_factory=list)


def new_decision_id(occurred_at: datetime | None = None) -> str:
    """生成决策编号：``D`` + 秒级 UTC 时间戳 + 进程盐 + 6 位自增序号。

    为什么不依赖数据库自增：决策编号要在**落库之前**就返回给业务方（同步响应），
    而自增 ID 要等到 flush 之后才有。

    为什么不是随机后缀：随机后缀的唯一性随并发量下降得很快（见文件顶部注释），
    而本函数必须保证"同一秒内任意条数都不重复"。进程内自增序列天然满足这一点，
    进程盐再把多进程（多 worker）的情形覆盖掉。长度 1+14+6+6 = 27 字符，
    远小于 ``decision_id`` 列的 varchar(48)，给可读性留了余地（时间戳可读）。
    """
    ts = (occurred_at or datetime.utcnow()).strftime(_ID_TIME_FORMAT)
    return f"D{ts}{_ID_PROCESS_SALT}{next(_ID_COUNTER):06d}"


def new_case_no(occurred_at: datetime | None = None) -> str:
    """生成案件编号（P1 实装合案时使用；P0 仅预留格式与调用点）。"""
    ts = (occurred_at or datetime.utcnow()).strftime("%Y%m%d%H%M%S")
    return f"C{ts}{secrets.randbelow(10000):04d}"


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
def read_idempotent(event_id: str) -> dict[str, Any] | None:
    """读幂等缓存；缓存不可用时返回 None（降级为「当作首次投递」）。

    降级方向的选择很关键：Redis 挂了时**不能拒绝请求**（否则 Redis 变成单点），
    也不能假装成功；当作首次投递、让 MySQL 的唯一约束兜底 ——
    重复投递会撞唯一约束并被转成 409，正确性由数据库保证。
    """
    try:
        raw = get_redis().get(idem_key(event_id))
    except RedisError as exc:
        logger.warning("幂等缓存读取失败，降级为直查数据库：%s", exc)
        return None
    if not raw:
        return None
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        # 缓存内容损坏：删掉它，避免这条事件永远无法重新决策
        logger.warning("幂等缓存内容损坏，已清除：event_id=%s", event_id)
        try:
            get_redis().delete(idem_key(event_id))
        except RedisError:
            pass
        return None


def write_idempotent(event_id: str, payload: dict[str, Any]) -> None:
    """写幂等缓存（尽力而为：失败只记日志，不影响已完成的决策）。"""
    try:
        get_redis().set(idem_key(event_id), orjson.dumps(payload).decode("utf-8"), ex=IDEM_TTL)
    except (RedisError, TypeError, ValueError) as exc:
        logger.warning("幂等缓存写入失败：event_id=%s err=%s", event_id, exc)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def handle_event(db: Session, *, event: Any, commit: bool = True) -> DecisionResult:
    """处理单条事件，返回决策结果。

    ``commit=False`` 供批量接口复用：批量时统一在最后提交，
    避免「200 条事件 = 200 次事务」把吞吐拉到不可用的量级。
    """
    started = time.perf_counter()
    event_id_ctx.set(event.event_id)

    # ---- 1. 业务校验（结构校验已由 Pydantic 在接口层完成）----
    outcome = event_validator.validate(db, event=event)
    warnings = list(outcome.warnings)

    # ---- 2. 幂等：命中则直接回放首次决策 ----
    cached = read_idempotent(event.event_id)
    if cached is not None:
        log_kv(logger, logging.INFO, "幂等命中，回放首次决策", event_id=event.event_id)
        return _replay(event_id=event.event_id, payload=cached, started=started)

    # 幂等第二层：Redis 缓存可能被清空（重启/FLUSHDB/TTL 到期），
    # 但"重复投递要返回首次决策"这条约定不能因此失效 —— 回源数据库还原。
    stored = decision_serializer.load_decision_payload(db, event.event_id)
    if stored is not None:
        log_kv(logger, logging.INFO, "幂等缓存缺失，已从数据库还原首次决策", event_id=event.event_id)
        write_idempotent(event.event_id, stored)
        return _replay(event_id=event.event_id, payload=stored, started=started)

    decision_id = new_decision_id(event.occurred_at)
    decision_id_ctx.set(decision_id)

    # ---- 3. 名单匹配 ----
    subject = {
        "user_id": event.user_id,
        "phone": event.phone,
        "ip": event.network.ip,
        "device_id": event.device.device_id,
        "address_hash": getattr(event.address, "address_hash", None) if event.address else None,
    }
    list_result = list_service.match(db, subject=subject, now=event.occurred_at)

    # ---- 4. 特征计算（名单命中时也照算，保证案件有画像依据）----
    payload = _enrich_payload(event)
    features = feature_engine.compute(
        db,
        event_type=event.event_type,
        user_id=event.user_id,
        device_id=event.device.device_id,
        ip=event.network.ip,
        address_hash=subject["address_hash"],
        phone=event.phone,
        payload=payload,
        occurred_at=event.occurred_at,
        list_flags=list_result.flags,
    )

    # ---- 5. 规则 + 模型（名单直判时跳过，但仍保留特征快照）----
    scene = SCENE_BY_EVENT_TYPE.get(event.event_type, "all")
    list_decided = list_result.decision is not None
    notes: list[str] = []
    if list_decided:
        rule_eval = rule_engine.RuleEvalResult()
        prediction = model_engine.ModelPrediction(enabled=False, reason="list_decided")
        notes.append("名单直判，规则与模型未参与决策")
    else:
        rule_eval = rule_engine.evaluate_rules(
            db,
            scene=scene,
            features=features.features,
            strict=get_str(db, "rule_strict_mode") == "true",
        )
        artifact, load_error = model_engine.load_active(db)
        prediction = model_engine.predict(features.features, artifact=artifact)
        if artifact is None:
            notes.append(f"模型未参与决策：{load_error or '未加载'}")

    # ---- 6. 融合仲裁 ----
    fusion_result = fusion.decide(
        fusion.FusionInput(
            rule_score=rule_eval.rule_score,
            model_score=prediction.model_score,
            list_decision=list_result.decision,
            list_conflict=list_result.conflict,
            challenge_hint=rule_eval.challenge_hint,
            review_threshold=get_int(db, "risk_threshold_review"),
            reject_threshold=get_int(db, "risk_threshold_reject"),
            alpha=get_float(db, "fusion_alpha"),
            mode=get_str(db, "fusion_mode"),
            model_version=prediction.version,
            model_disabled=not prediction.enabled,
        )
    )
    notes.extend(fusion_result.notes)
    notes.extend(_build_explain_notes(fusion_result, list_result))

    # ---- 7. 落库（先库后缓存：见模块文档第 2/3 条）----
    snapshot, stray = decision_serializer.snapshot_payload(features.features)
    if stray:
        # 特征字典里出现了未注册的键：说明有代码往 features 里塞了东西而没登记声明。
        # 不抛错（决策本身是有效的），但必须留下痕迹 —— 否则快照与模型向量会
        # 悄悄长出"没人声明过的列"，训练与推理口径随之漂移。
        logger.warning(
            "特征字典包含未注册的键，已归入 context：%s", sorted(stray.keys())
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    case_no: str | None = None
    if fusion.needs_case(fusion_result):
        # P0 未实装案件表：这里只留痕，P1 接入 rc_case 后改为真实合案
        notes.append("P0 未生成案件（案件流转属 P1 阶段）")

    decision_row = decision_serializer.build_decision_row(
        decision_id=decision_id,
        event_id=event.event_id,
        fusion=fusion_result,
        hit_count=len(rule_eval.hits),
        latency_ms=latency_ms,
        case_no=case_no,
    )
    event_row = _build_event_row(event, payload=payload, latency_ms=latency_ms, biz_no=_biz_no(event))
    snapshot_row = RcFeatureSnapshot(
        event_id=event.event_id,
        features=snapshot,
        window_profile=_window_profile(db),
        calc_cost_ms=features.cost_ms,
        feature_version=features.feature_version,
    )
    hit_rows = decision_serializer.build_hit_rows(decision_id=decision_id, hits=rule_eval.hits)
    contribution_rows = decision_serializer.build_contribution_rows(
        decision_id=decision_id, contributions=prediction.contributions
    )

    try:
        db.add(event_row)
        db.add(snapshot_row)
        db.add(decision_row)
        for row in hit_rows:
            db.add(row)
        for row in contribution_rows:
            db.add(row)
        db.flush()
        if commit:
            db.commit()
    except IntegrityError as exc:
        # 唯一约束兜底。**必须先分辨撞的是哪个约束**：
        #
        # * 撞 ``rc_event.event_id``：并发下两个相同 event_id 的请求都通过了幂等检查，
        #   属于正常的"重复投递"，回放首次决策即可，调用方不该收到错误；
        # * 撞 ``rc_decision.decision_id``：本系统自己的编号生成重复，是**服务端缺陷**。
        #   若把它也翻译成 EVENT_DUPLICATED，会得到一个指向错误方向的提示 ——
        #   调用方看到"请使用新的 event_id"，照做之后照样失败（他改的不是问题所在）。
        #   这正是本函数曾经踩过的坑（旧实现用 3 位随机后缀，同秒并发即撞车）。
        db.rollback()
        stored = decision_serializer.load_decision_payload(db, event.event_id)
        if stored is not None:
            # 并发重复投递：另一个请求已经写成功了，回放它的结果即可，
            # 不必把"你重试得太快"变成一次 409 让调用方自己处理。
            write_idempotent(event.event_id, stored)
            return _replay(event_id=event.event_id, payload=stored, started=started)
        if "uq_rc_decision_decision_id" in str(exc.orig):
            logger.error(
                "决策编号冲突，疑似编号生成器退化：decision_id=%s event_id=%s",
                decision_id,
                event.event_id,
            )
            raise BusinessError(
                "决策编号生成冲突，请重试；若持续出现请检查编号生成器",
                code=ErrorCode.SYSTEM_ERROR,
                detail={"decision_id": decision_id, "event_id": event.event_id},
            ) from exc
        raise ConflictError(
            "事件编号已存在（并发重复投递），请使用新的 event_id",
            code=ErrorCode.EVENT_DUPLICATED,
            field="event_id",
            detail={"event_id": event.event_id},
        ) from exc

    # ---- 8. 事件入条带（特征窗口的数据来源）----
    _write_windows(event, payload=payload)

    response = decision_serializer.to_response(
        decision_row=decision_row,
        event_type=event.event_type,
        user_id=event.user_id,
        occurred_at=event.occurred_at,
        biz_no=_biz_no(event),
        features=snapshot,
        context=_build_context(event, payload=payload, stray=stray),
        missing_fields=features.missing_fields,
        hits=rule_eval.hits,
        contributions=prediction.contributions,
        list_hits=list_result.hit_summary(),
        notes=notes,
        warnings=warnings,
        cost_ms=features.cost_ms,
    )
    response["source"] = event.source

    # ---- 9. 幂等缓存（放最后：内容与库里的决策完全一致）----
    write_idempotent(event.event_id, response)

    cost_ms = int((time.perf_counter() - started) * 1000)
    log_kv(
        logger,
        logging.INFO,
        "决策完成",
        decision_id=decision_id,
        action=fusion_result.action,
        risk_score=fusion_result.risk_score,
        rule_score=fusion_result.rule_score,
        model_score=fusion_result.model_score,
        hit_count=len(rule_eval.hits),
        cost_ms=cost_ms,
    )
    return DecisionResult(
        decision_id=decision_id,
        event_id=event.event_id,
        accepted=True,
        duplicated=False,
        response=response,
        case_no=case_no,
        cost_ms=cost_ms,
        notes=notes,
    )


@dataclass
class BatchOutcome:
    """批量处理结果：成功的决策 + 逐条失败信息。"""

    results: list[DecisionResult] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


def handle_events(db: Session, *, events: list[Any]) -> BatchOutcome:
    """批量处理：**逐条独立事务**，单条失败不影响其他条目。

    为什么不做「整批一个事务」：批量接入的业务语义是"尽可能多地接收"。
    若整批同生共死，第 200 条的一个字段错误会让前 199 条全部回滚，
    业务方还得自己按条重试、并承担"哪些成功了"的不确定性 ——
    这正是批量接口最容易踩的坑。逐条提交的代价是 200 次事务（本地约 0.3~1 秒），
    对演示场景完全可接受。
    """
    outcome = BatchOutcome()
    for index, event in enumerate(events):
        try:
            outcome.results.append(handle_event(db, event=event))
        except BusinessError as exc:
            db.rollback()
            outcome.errors.append(
                {"index": index, "event_id": getattr(event, "event_id", None), **exc.to_dict()}
            )
        except Exception as exc:  # noqa: BLE001 - 单条意外失败不应中断整批
            db.rollback()
            logger.exception("批量接入中出现未预期异常：index=%s", index)
            outcome.errors.append(
                {
                    "index": index,
                    "event_id": getattr(event, "event_id", None),
                    "code": ErrorCode.SYSTEM_ERROR,
                    "message": "该条事件处理失败，请重试",
                    "field": None,
                    "detail": type(exc).__name__,
                }
            )
    return outcome


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _replay(*, event_id: str, payload: dict[str, Any], started: float) -> DecisionResult:
    """把"已经决策过的结果"包装成当前请求的响应（幂等回放）。"""
    payload = dict(payload)
    payload["from_cache"] = True
    return DecisionResult(
        decision_id=str(payload.get("decision_id")),
        event_id=event_id,
        accepted=True,
        duplicated=True,
        response=payload,
        case_no=payload.get("case_no"),
        cost_ms=int((time.perf_counter() - started) * 1000),
    )


def _build_explain_notes(fusion_result: fusion.FusionResult, list_result: Any) -> list[str]:
    """生成「为什么是这个动作」的一句话说明（落进响应的 notes）。"""
    notes: list[str] = []
    if list_result.decision == "Reject":
        black = [hit for hit in list_result.hits if hit.list_type == "black"]
        if black:
            notes.append(DecisionReason.list_black_hit(dimension_cn(black[0].dimension), len(black)))
        if list_result.conflict:
            notes.append(DecisionReason.list_conflict(list_result.policy))
    elif list_result.decision == "Pass":
        white = [hit for hit in list_result.hits if hit.list_type == "white"]
        if white:
            notes.append(DecisionReason.list_white_hit(dimension_cn(white[0].dimension), len(white)))
        if list_result.conflict:
            notes.append(DecisionReason.list_conflict(list_result.policy))
    return notes


def _build_event_row(event: Any, *, payload: dict[str, Any], latency_ms: int, biz_no: str | None) -> RcEvent:
    """组装 rc_event 行。

    上下文（context）与特征（features）的区别见 decision_serializer.to_response：
    前者是"当时看到的事实"，只给人看；后者参与打分。

    设备指纹与 IP 归属地等字段**原样落库**（device_fingerprint / ip_region）：
    它们是「当时看到的事实」，事后重放与举证都依赖它们，
    因此不做任何规范化改写（只在 payload 里附加业务表的权威字段，见 _enrich_payload）。
    """
    return RcEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        user_id=event.user_id,
        phone=event.phone,
        device_id=event.device.device_id,
        device_fingerprint=event.device.fingerprint or {},
        ip=event.network.ip,
        ip_region=event.network.ip_region,
        address_hash=getattr(event.address, "address_hash", None) if event.address else None,
        biz_no=biz_no,
        occurred_at=event.occurred_at,
        source=event.source,
        payload=payload,
        latency_ms=latency_ms,
    )


def _enrich_payload(event: Any) -> dict[str, Any]:
    """把事件信封里的字段并入 payload，供特征引擎与规则表达式使用。

    为什么这么做：特征引擎与规则都只认一个扁平字典（features），
    而「是否为代理 IP」「设备指纹里的模拟器标记」「IP 归属地」这些字段来自信封。
    在网关这一处做合并，比让特征引擎同时接收信封与 payload 两个结构更简单，
    也让规则表达式可以用统一路径引用（如 payload.is_proxy == true）。
    """
    payload: dict[str, Any] = dict(event.payload or {})
    payload.setdefault("is_proxy", event.network.is_proxy)
    payload.setdefault("ip_region", event.network.ip_region)
    payload.setdefault("fingerprint", event.device.fingerprint or {})
    payload.setdefault("device_id", event.device.device_id)
    payload.setdefault("ip", event.network.ip)
    if event.address is not None:
        payload.setdefault("address_hash", event.address.address_hash)
        payload.setdefault("address_region", event.address.region)
        payload.setdefault("receiver_name", event.address.receiver_name)
    return payload


def _biz_no(event: Any) -> str | None:
    """取关联业务单据号（订单号/退款单号/券编号）。"""
    field = BIZ_NO_FIELD.get(event.event_type)
    if not field:
        return None
    value = (event.payload or {}).get(field)
    return str(value)[:32] if value else None


def _build_context(event: Any, *, payload: dict[str, Any], stray: dict[str, Any]) -> dict[str, Any]:
    """组装展示用上下文（不参与打分，见 decision_serializer.to_response 的说明）。

    手机号**脱敏后再进上下文**：决策响应会被前端展示、被日志采集、
    也可能被业务方存档，明文手机号在里面流转没有必要
    （审核员要看的是"哪个账号"，不是号码本身）。落库的 rc_event 仍存明文，
    因为那是取证级别的原始记录，由数据库权限与审计保护。
    """
    from app.core.logging import mask_phone

    return {
        **stray,
        "payload": payload,
        "event": {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "user_id": event.user_id,
            "phone": mask_phone(event.phone),
            "device_id": event.device.device_id,
            "device_fingerprint": event.device.fingerprint or {},
            "ip": event.network.ip,
            "ip_region": event.network.ip_region,
            "is_proxy": event.network.is_proxy,
            "address_hash": getattr(event.address, "address_hash", None) if event.address else None,
            "source": event.source,
            "occurred_at": event.occurred_at.isoformat(),
        },
    }


def _window_profile(db: Session) -> str:
    """窗口档位标识（落 rc_feature_snapshot.window_profile，用于复现特征口径）。"""
    try:
        windows = feature_engine.active_windows(db)
    except Exception:  # noqa: BLE001
        windows = feature_engine.DEFAULT_WINDOWS
    return "+".join(sorted(windows.keys())) or "default"


def _write_windows(event: Any, *, payload: dict[str, Any]) -> None:
    """把事件写进「多实体 × 多窗口」条带。

    **一次写入、一段主体标识**：每个 (实体, 事件类型, 窗口) 只写一个成员，
    主体标识放在 member 的第四段（见 window_store.pack_member 的说明）。
    这里刻意不做"raw 成员 + 前缀成员各写一次"的双写 ——
    双写会让 ``device_coupon_cnt_1h`` 这类**计数**特征把每个事件数两遍，
    而计数翻倍不会报错，只会让规则阈值静默减半。

    需要主体标识的实体来自注册表（``distinct_subject_entities``），
    与读取侧同源，避免两边各维护一份清单后漂移。
    """
    try:
        windows = feature_engine.DEFAULT_WINDOWS
        ts_ms = to_ms(event.occurred_at)
        amount = _amount_of(event, payload)

        entities: list[tuple[str, str]] = [(window_store.ENTITY_USER, event.user_id)]
        entities.append((window_store.ENTITY_PHONE, event.phone))
        entities.append((window_store.ENTITY_DEVICE, event.device.device_id))
        entities.append((window_store.ENTITY_IP, event.network.ip))
        address_hash = getattr(event.address, "address_hash", None) if event.address else None
        if address_hash:
            entities.append((window_store.ENTITY_ADDRESS, address_hash))

        # 主体标识：目前全部聚簇特征都是"按用户去重同设备/同 IP/同地址"，
        # 因此主体就是当前事件的用户；若将来出现"按设备去重用户"这类反向聚簇，
        # 这里需要按 spec 决定主体取值（注册表已带 subject_prefix，扩展点明确）。
        entity_subjects = {
            entity: f"{prefix}:{event.user_id}"
            for entity, prefix in feature_engine.distinct_subject_entities().items()
        }

        window_store.add_event_to_entities(
            entities=entities,
            event_type=event.event_type,
            windows=windows,
            ts_ms=ts_ms,
            event_id=event.event_id,
            amount=amount,
            entity_subjects=entity_subjects,
        )
    except Exception as exc:  # noqa: BLE001 - 条带是缓存，失败不能影响已落库的决策
        logger.error("事件条带写入失败（特征窗口将缺少该事件）：event_id=%s err=%s", event.event_id, exc)


def _amount_of(event: Any, payload: dict[str, Any]) -> float:
    """取该事件类型对应的金额（用于窗口求和特征）。"""
    field = AMOUNT_FIELD.get(event.event_type)
    if not field:
        return 0.0
    try:
        return float(payload.get(field) or 0.0)
    except (TypeError, ValueError):
        return 0.0
