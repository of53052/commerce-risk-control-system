"""案件服务：建案、合案、状态机迁移、列表与详情取数（docs/PRD.md §9.1/§9.2）。

## 合案（建案）策略

触发条件：融合结论为 ``Review`` 或 ``Reject``（``fusion.needs_case``）。
合案键：``(主体类型, 主体值, 场景)``，窗口 ``sys_config.case_merge_window_minutes``（默认 30 分钟）。

* 窗口内存在**未结案件**（``pending`` / ``processing``）→ 累加，不新建；
* 否则新建。**窗口外允许同主体同场景存在多个案件** —— 这是刻意的：
  "同一个人 10 点触发一次、11 点又触发一次"是两个独立的待办事项，
  强行并成一个会让"上一次已经处置完毕"的结论被新证据悄悄污染。

## 并发：为什么是「进程内按主体加锁 + 原子累加」而不是只靠 FOR UPDATE

架构文档 §7.6 给的示意是 ``SELECT ... FOR UPDATE``。它的**真实能力边界**要说清：

* 候选案件**存在**时，行锁能正确串行化"累加"这一步；
* 候选案件**不存在**时，没有任何行可锁。两个并发请求会各自查到"无候选"，
  然后各建一个案件 —— 这正是"同一用户出现两个案件"这类脏数据最常见的来源，
  而它偏偏发生在流量突增（作弊场景批量灌事件）的时候。

MySQL 在 REPEATABLE READ 下确实会对索引区间加间隙锁，一定程度上能挡住这个窗口，
但间隙锁的行为依赖隔离级别与执行计划（一旦优化器选择全表扫描，锁的范围就完全变了），
把它当作**主要**保障是在赌执行计划。因此这里的策略分两层：

1. **进程内按 ``(主体, 场景)`` 加锁**（主要保障）：同主体同场景的建案/合案串行执行，
   结果是确定的，不依赖数据库隔离级别；
2. **``SELECT ... FOR UPDATE`` + ``UPDATE ... SET hit_cnt = hit_cnt + n``**（第二层）：
   即便将来起了第二个进程，累加也不会丢更新（新建重复案件的问题见下方限制）。

**已知限制（诚实记录）**：多进程部署下 1 失效，可能出现同一主体同场景的重复新案件。
多进程场景的正解是"活跃案件唯一键"（生成列 = 未结案件的 ``主体:场景``，唯一索引）
或 MySQL ``GET_LOCK``，两者都在 P2 之后评估（见 docs/ARCHITECTURE.md §16）。
本系统当前是单实例部署，与审计哈希链的串行化前提相同 —— 这一点在
``audit_service`` 里已经写明，案件服务沿用同一假设而不是各自发明一套。

## 状态机

所有迁移都用**条件更新**（``UPDATE ... WHERE case_no=? AND status=?``），
``rowcount == 0`` 即冲突。先查后改的写法在两个人同时点"接手"时会让后手
拿到"我也查到了 pending"的结果，然后两个人同时进入处理态 ——
这类 bug 在单机演示里几乎不会出现，却正好会在答辩现场的双人演示中出现。
"""

from __future__ import annotations

import itertools
import logging
import secrets
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case as sa_case
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.biz import BizCustomer, BizOrder, BizRefund
from app.models.case import (
    ALL_STATUSES,
    CASE_ARCHIVED,
    CASE_CLOSED,
    CASE_DISPOSED,
    CASE_PENDING,
    CASE_PROCESSING,
    OPEN_STATUSES,
    SUBJECT_USER,
    RcCase,
    RcCaseEvent,
)
from app.models.decision import RcDecision, RcDecisionHit, RcModelContribution
from app.models.event import RcEvent, RcFeatureSnapshot
from app.models.rclist import STATUS_ACTIVE as LIST_STATUS_ACTIVE, RcListEntry
from app.services import audit_service
from app.services.config_service import get_int
from app.services.errors import ConflictError, ErrorCode, NotFoundError

logger = logging.getLogger("app.services.case_service")

# ---- 案件编号生成 ----
#
# 与 ``event_gateway.new_decision_id`` 同源的三段式：时间戳 + 进程盐 + 进程内自增序号。
#
# 为什么不用「秒级时间戳 + 4 位随机数」（P0 预留的写法）：那个方案在同一秒内
# 并发建案超过约 40 条时就会撞唯一索引，而撞车的表现是**建案失败导致事件接入报错**
# —— 决策本身是好的，却因为案件编号生成器退化而失败。决策编号已经踩过这个坑
# （见 event_gateway 文件头的长注释），案件编号没有理由再踩一次。
_ID_TIME_FORMAT = "%Y%m%d%H%M%S"
_ID_PROCESS_SALT = secrets.token_hex(3).upper()
_ID_COUNTER = itertools.count(1)

# 风险等级排序：用于合案时取"更高的那个等级"。
# 注意不能直接用字符串比较（high < low < mid 按 ASCII 排序），
# 否则 mid 案件合入 high 事件后会被降级成 mid。
_RISK_ORDER: dict[str, int] = {"low": 1, "mid": 2, "high": 3}

# 合案默认窗口（分钟）：与 config_service.CONFIG_SPECS 的默认值保持一致。
# 这里再写一份是为了让本模块**不依赖数据库**也能工作（配置表不可用时回退）。
_DEFAULT_MERGE_WINDOW_MINUTES = 30

# 进程内合案锁表。上限用于防止长时间运行后表无限增长：
# 键的数量 = 活跃主体数，正常情况下远小于这个值。
_MERGE_LOCKS: dict[str, threading.Lock] = {}
_MERGE_LOCKS_GUARD = threading.Lock()
_MERGE_LOCKS_MAX = 4096


@dataclass(frozen=True)
class Actor:
    """操作者。服务层不导入 FastAPI（见 app/services/errors.py 的说明），
    因此这里用独立的值对象，由接口层负责从 ``Operator`` 转换过来。
    """

    id: int | None
    name: str
    role: str

    @classmethod
    def system(cls) -> "Actor":
        """系统动作（自动建案）的操作者。"""
        return cls(id=None, name="system", role="system")


@dataclass
class CaseMergeResult:
    """一次建案/合案的结果。"""

    case_no: str
    created: bool
    event_cnt: int
    hit_cnt: int
    max_score: int


def new_case_no(occurred_at: datetime | None = None) -> str:
    """生成案件编号：``C`` + 秒级 UTC 时间戳 + 进程盐 + 6 位自增序号。

    长度 1+14+6+6 = 27，小于 ``case_no`` 列的 varchar(32)。
    """
    ts = (occurred_at or datetime.utcnow()).strftime(_ID_TIME_FORMAT)
    return f"C{ts}{_ID_PROCESS_SALT}{next(_ID_COUNTER):06d}"


@contextmanager
def _subject_merge_lock(key: str) -> Iterator[None]:
    """按主体取一把进程内锁（见模块文档的并发策略）。"""
    with _MERGE_LOCKS_GUARD:
        lock = _MERGE_LOCKS.get(key)
        if lock is None:
            if len(_MERGE_LOCKS) >= _MERGE_LOCKS_MAX:
                # 只回收"当前没被人持有"的锁；持有中的锁被移除会让两个线程
                # 各自持有一把不同的锁，锁就白加了。
                idle = [item for item, candidate in _MERGE_LOCKS.items() if not candidate.locked()]
                for item in idle[: _MERGE_LOCKS_MAX // 2]:
                    _MERGE_LOCKS.pop(item, None)
            lock = threading.Lock()
            _MERGE_LOCKS[key] = lock
    with lock:
        yield


def _merge_window_minutes(db: Session) -> int:
    """读合案窗口（分钟），坏配置一律兜到下限 1 分钟。

    0 或负数会让"窗口"退化成"永不合案"：每条 Review/Reject 都新建案件，
    工作台很快被同一主体的重复案件淹没，而配置界面上看不出任何异常。
    """
    try:
        value = int(get_int(db, "case_merge_window_minutes"))
    except Exception as exc:  # noqa: BLE001 - 配置不可用不该阻断建案
        logger.warning("读取合案窗口失败，回退默认值：%s", exc)
        return _DEFAULT_MERGE_WINDOW_MINUTES
    if value < 1:
        logger.warning("合案窗口配置异常（%s 分钟），已按 1 分钟处理", value)
        return 1
    return value


def merge_or_create(
    db: Session,
    *,
    subject_value: str,
    scene: str,
    event_id: str,
    decision_id: str,
    event_type: str,
    action: str,
    risk_level: str,
    risk_score: int,
    hit_count: int,
    biz_no: str | None,
    occurred_at: datetime,
    subject_type: str = SUBJECT_USER,
    now: datetime | None = None,
) -> CaseMergeResult:
    """建案或合案（由事件网关在决策落库的同一事务里调用）。

    **必须与决策落库同事务**：案件是决策的从属产物。若案件先提交、决策随后失败，
    工作台就会出现"点进去没有任何决策可看"的空壳案件；反过来（决策成功、案件丢失）
    则更糟 —— 高风险事件没人审。同事务 + 唯一约束保证两者要么都在，要么都不在。
    """
    now = now or utcnow()
    # 合案窗口以**本次事件的业务时间**为锚点，而不是处理时刻（now）：
    # 窗口回答的是"这两次触发在业务上算不算同一起事件"，与风控系统什么时候
    # 收到它无关。用处理时刻做锚点时，补投递（业务时间早于处理时刻）与
    # 时钟偏差会让判断结果随"服务器负载/重试延迟"漂移。
    anchor = occurred_at or now
    window_start = anchor - timedelta(minutes=_merge_window_minutes(db))
    key = f"{subject_type}:{subject_value}:{scene}"

    with _subject_merge_lock(key):
        candidate = db.execute(
            select(RcCase)
            .where(
                RcCase.subject_type == subject_type,
                RcCase.subject_value == subject_value,
                RcCase.scene == scene,
                RcCase.status.in_(OPEN_STATUSES),
                RcCase.last_at >= window_start,
            )
            .order_by(RcCase.last_at.desc())
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()

        if candidate is None:
            return _create_case(
                db,
                subject_type=subject_type,
                subject_value=subject_value,
                scene=scene,
                event_id=event_id,
                decision_id=decision_id,
                event_type=event_type,
                action=action,
                risk_level=risk_level,
                risk_score=risk_score,
                hit_count=hit_count,
                biz_no=biz_no,
                occurred_at=occurred_at,
            )

        return _merge_into_case(
            db,
            case=candidate,
            event_id=event_id,
            decision_id=decision_id,
            event_type=event_type,
            scene=scene,
            action=action,
            risk_level=risk_level,
            risk_score=risk_score,
            hit_count=hit_count,
            biz_no=biz_no,
            occurred_at=occurred_at,
        )


def _create_case(
    db: Session,
    *,
    subject_type: str,
    subject_value: str,
    scene: str,
    event_id: str,
    decision_id: str,
    event_type: str,
    action: str,
    risk_level: str,
    risk_score: int,
    hit_count: int,
    biz_no: str | None,
    occurred_at: datetime,
) -> CaseMergeResult:
    """新建案件 + 挂载首个事件 + 写审计。"""
    case_no = new_case_no(occurred_at)
    row = RcCase(
        case_no=case_no,
        subject_type=subject_type,
        subject_value=subject_value,
        scene=scene,
        status=CASE_PENDING,
        risk_level=risk_level,
        max_score=int(risk_score),
        hit_cnt=int(hit_count),
        event_cnt=1,
        first_at=occurred_at,
        last_at=occurred_at,
        last_event_id=event_id,
        last_decision_id=decision_id,
    )
    db.add(row)
    db.flush()
    db.add(
        _case_event_row(
            case_no=case_no,
            event_id=event_id,
            decision_id=decision_id,
            event_type=event_type,
            scene=scene,
            action=action,
            risk_level=risk_level,
            risk_score=risk_score,
            hit_count=hit_count,
            biz_no=biz_no,
            occurred_at=occurred_at,
        )
    )
    db.flush()

    # 建案写审计、合案不写：建案是低频事件（一条新案件 = 一个待办事项），
    # 而合案发生在决策热路径上、同一主体可能一分钟内合几十次，逐次写审计
    # 会让哈希链的串行写锁成为吞吐瓶颈。合案的痕迹由 rc_case_event 承担
    # （每次合案都会多一行，工作台时间线上看得见），信息没有丢。
    audit_service.write(
        db,
        action="case_create",
        actor_id="system",
        actor_name="system",
        role="system",
        target_type="case",
        target_id=case_no,
        after={
            "case_no": case_no,
            "subject_type": subject_type,
            "subject_value": subject_value,
            "scene": scene,
            "risk_level": risk_level,
            "risk_score": int(risk_score),
            "decision_id": decision_id,
            "event_id": event_id,
        },
        reason=f"决策动作为 {action}，自动建案",
    )
    return CaseMergeResult(
        case_no=case_no, created=True, event_cnt=1, hit_cnt=int(hit_count), max_score=int(risk_score)
    )


def _merge_into_case(
    db: Session,
    *,
    case: RcCase,
    event_id: str,
    decision_id: str,
    event_type: str,
    scene: str,
    action: str,
    risk_level: str,
    risk_score: int,
    hit_count: int,
    biz_no: str | None,
    occurred_at: datetime,
) -> CaseMergeResult:
    """把新事件累加进既有案件。

    累加用 SQL 表达式而不是"读出来 +1 再写回"：后者在并发下会丢更新，
    而丢的是 ``hit_cnt`` / ``event_cnt`` 这类"越大越可信"的计数 ——
    审核员看到一个比实际小的数字，会低估这个账号的风险积累。
    """
    # last_at 取"既有值与新事件时间的较大者"：晚到的事件（补投递）不能让
    # 末次触发时间倒退，否则下一次合案判断会把窗口算到过去，凭空多出一个案件。
    rank_expr = sa_case(_RISK_ORDER, value=RcCase.risk_level, else_=0)
    db.execute(
        update(RcCase)
        .where(RcCase.id == case.id)
        .values(
            hit_cnt=RcCase.hit_cnt + int(hit_count),
            event_cnt=RcCase.event_cnt + 1,
            max_score=func.greatest(RcCase.max_score, int(risk_score)),
            risk_level=sa_case(
                (rank_expr >= _RISK_ORDER.get(risk_level, 0), RcCase.risk_level),
                else_=risk_level,
            ),
            last_at=func.greatest(RcCase.last_at, occurred_at),
            last_event_id=event_id,
            last_decision_id=decision_id,
        )
    )
    db.add(
        _case_event_row(
            case_no=case.case_no,
            event_id=event_id,
            decision_id=decision_id,
            event_type=event_type,
            scene=scene,
            action=action,
            risk_level=risk_level,
            risk_score=risk_score,
            hit_count=hit_count,
            biz_no=biz_no,
            occurred_at=occurred_at,
        )
    )
    db.flush()
    # 上面是 Core UPDATE，ORM 里的对象还是旧值；刷新后再返回，
    # 否则事件网关拿到的 event_cnt 是合案前的数字。
    db.refresh(case)
    return CaseMergeResult(
        case_no=case.case_no,
        created=False,
        event_cnt=case.event_cnt,
        hit_cnt=case.hit_cnt,
        max_score=case.max_score,
    )


def _case_event_row(
    *,
    case_no: str,
    event_id: str,
    decision_id: str,
    event_type: str,
    scene: str,
    action: str,
    risk_level: str,
    risk_score: int,
    hit_count: int,
    biz_no: str | None,
    occurred_at: datetime,
) -> RcCaseEvent:
    """组装案件-事件关联行（时间线与处置联动的数据源）。"""
    return RcCaseEvent(
        case_no=case_no,
        event_id=event_id,
        decision_id=decision_id,
        event_type=event_type,
        scene=scene,
        action=action,
        risk_level=risk_level,
        risk_score=int(risk_score),
        hit_count=int(hit_count),
        biz_no=biz_no,
        occurred_at=occurred_at,
    )


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def get_case(db: Session, case_no: str) -> RcCase:
    """按案件号取案件，不存在则 404。"""
    row = db.execute(select(RcCase).where(RcCase.case_no == case_no)).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            f"案件不存在：{case_no}", code=ErrorCode.CASE_NOT_FOUND, detail={"case_no": case_no}
        )
    return row


@dataclass
class CaseQuery:
    """案件列表筛选条件（工作台左栏）。"""

    status: str | None = None
    risk_level: str | None = None
    scene: str | None = None
    subject_type: str | None = None
    handler_id: int | None = None
    keyword: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


def list_cases(
    db: Session, *, query: CaseQuery, page_no: int = 1, size: int = 20
) -> tuple[list[RcCase], int]:
    """分页查询案件列表，返回 (当页数据, 总数)。

    排序固定 ``last_at DESC, id DESC``：``last_at`` 有大量重复值（批量场景同一秒
    建多条），只按它排序时 MySQL 的分页结果会在页间抖动 —— 同一案件既可能出现在
    第 1 页也可能出现在第 2 页，而用 ``id`` 兜底后顺序是全序的。
    """
    conditions = []
    if query.status:
        conditions.append(RcCase.status == query.status)
    if query.risk_level:
        conditions.append(RcCase.risk_level == query.risk_level)
    if query.scene:
        conditions.append(RcCase.scene == query.scene)
    if query.subject_type:
        conditions.append(RcCase.subject_type == query.subject_type)
    if query.handler_id is not None:
        conditions.append(RcCase.handler_id == query.handler_id)
    if query.keyword:
        # 关键词同时匹配案件号与主体值：审核员手上可能只有其中任意一个
        # （客服转过来的是账号，工作台转过来的是案件号）。
        like = f"%{query.keyword.strip()}%"
        conditions.append(or_(RcCase.case_no.like(like), RcCase.subject_value.like(like)))
    if query.start_at:
        conditions.append(RcCase.last_at >= query.start_at)
    if query.end_at:
        conditions.append(RcCase.last_at <= query.end_at)

    total = db.execute(
        select(func.count()).select_from(RcCase).where(*conditions)
    ).scalar_one()
    rows = (
        db.execute(
            select(RcCase)
            .where(*conditions)
            .order_by(RcCase.last_at.desc(), RcCase.id.desc())
            .offset(max(0, (page_no - 1) * size))
            .limit(size)
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


def list_case_events(db: Session, case_no: str) -> list[RcCaseEvent]:
    """案件的事件时间线（按业务时间正序）。"""
    return list(
        db.execute(
            select(RcCaseEvent)
            .where(RcCaseEvent.case_no == case_no)
            .order_by(RcCaseEvent.occurred_at.asc(), RcCaseEvent.id.asc())
        )
        .scalars()
        .all()
    )


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def claim(db: Session, *, case_no: str, actor: Actor, now: datetime | None = None, commit: bool = True) -> RcCase:
    """接手案件：``pending -> processing``（条件更新，冲突即报错）。

    只允许从 ``pending`` 接手。``processing`` 的案件不能"抢过来"：
    两个审核员同时研判同一案件时，抢走的一方看着同一份证据得出相反结论，
    处置结果互相覆盖 —— 与其做"转移处理人"的复杂语义，不如让后手看到
    "案件已被 XXX 接手"并自行沟通（docs/PRD.md §18 风险 5 的应对）。
    """
    now = now or utcnow()
    affected = db.execute(
        update(RcCase)
        .where(RcCase.case_no == case_no, RcCase.status == CASE_PENDING)
        .values(
            status=CASE_PROCESSING,
            handler=actor.name,
            handler_id=actor.id,
            claimed_at=now,
        )
    ).rowcount

    if affected == 0:
        # 条件更新影响 0 行有两种原因：案件不存在，或状态已是别的值。
        # 这里再查一次**只为给出准确提示**，迁移本身已经由上面的 rowcount 判定完毕
        # （不是"先查后改"）。
        row = get_case(db, case_no)
        if row.status == CASE_PROCESSING:
            message = f"案件已被 {row.handler or '他人'} 接手"
        else:
            message = f"案件当前状态为 {row.status}，无法接手"
        raise ConflictError(
            message,
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": row.status, "handler": row.handler},
        )

    row = get_case(db, case_no)
    audit_service.write(
        db,
        action="case_claim",
        actor_id=str(actor.id) if actor.id is not None else actor.name,
        actor_name=actor.name,
        role=actor.role,
        target_type="case",
        target_id=case_no,
        before={"status": CASE_PENDING, "handler": None},
        after={"status": CASE_PROCESSING, "handler": actor.name},
        reason="审核员接手案件",
    )
    if commit:
        db.commit()
    return row


def archive(
    db: Session, *, case_nos: list[str], actor: Actor, now: datetime | None = None, commit: bool = True
) -> list[dict[str, Any]]:
    """归档案件：``disposed -> archived``（支持批量，逐条独立判定）。

    批量接口**不做"全成功或全失败"**：归档是收拾性的低风险动作，
    把 10 条里的 1 条冲突（比如已被别人处置但状态不同）变成整批失败，
    会让操作者反复重试并最终手工逐条点 —— 与"批量"的初衷相反。
    因此返回逐条结果，前端按结果提示"成功 9 条，跳过 1 条（原因）"。
    """
    now = now or utcnow()
    results: list[dict[str, Any]] = []
    for case_no in case_nos:
        affected = db.execute(
            update(RcCase)
            .where(RcCase.case_no == case_no, RcCase.status == CASE_DISPOSED)
            .values(status=CASE_ARCHIVED, archived_at=now)
        ).rowcount
        if affected == 0:
            row = db.execute(select(RcCase).where(RcCase.case_no == case_no)).scalar_one_or_none()
            if row is None:
                results.append({"case_no": case_no, "ok": False, "reason": "案件不存在"})
            else:
                results.append(
                    {
                        "case_no": case_no,
                        "ok": False,
                        "reason": f"当前状态为 {row.status}，仅已处置案件可归档",
                    }
                )
            continue

        audit_service.write(
            db,
            action="case_archive",
            actor_id=str(actor.id) if actor.id is not None else actor.name,
            actor_name=actor.name,
            role=actor.role,
            target_type="case",
            target_id=case_no,
            before={"status": CASE_DISPOSED},
            after={"status": CASE_ARCHIVED},
            reason="管理员归档案件",
        )
        results.append({"case_no": case_no, "ok": True, "reason": None})

    if commit:
        db.commit()
    return results


def close(
    db: Session,
    *,
    case_no: str,
    actor: Actor,
    reason: str,
    now: datetime | None = None,
    commit: bool = True,
) -> RcCase:
    """管理员强制关闭：``pending`` / ``processing`` -> ``closed``（必须填原因）。

    "强制关闭"是异常兜底：误报建了案、主体联系不上、重复建案等。
    它绕过了正常的处置流程，因此原因必填 —— 审计上必须能回答
    "这个高风险案件为什么没人处置就结束了"。
    """
    now = now or utcnow()
    affected = db.execute(
        update(RcCase)
        .where(RcCase.case_no == case_no, RcCase.status.in_((CASE_PENDING, CASE_PROCESSING)))
        .values(status=CASE_CLOSED, closed_at=now, close_reason=reason)
    ).rowcount
    if affected == 0:
        row = get_case(db, case_no)
        raise ConflictError(
            f"案件当前状态为 {row.status}，无法关闭",
            code=ErrorCode.STATE_CONFLICT,
            detail={"case_no": case_no, "status": row.status},
        )

    row = get_case(db, case_no)
    audit_service.write(
        db,
        action="case_close",
        actor_id=str(actor.id) if actor.id is not None else actor.name,
        actor_name=actor.name,
        role=actor.role,
        target_type="case",
        target_id=case_no,
        after={"status": CASE_CLOSED, "close_reason": reason},
        reason=reason,
    )
    if commit:
        db.commit()
    return row


def status_counts(db: Session) -> dict[str, int]:
    """各状态案件数（工作台的状态筛选标签上显示计数）。"""
    rows = db.execute(
        select(RcCase.status, func.count()).group_by(RcCase.status)
    ).all()
    counts = {status: 0 for status in ALL_STATUSES}
    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


# --------------------------------------------------------------------------- #
# 详情组装（工作台中栏）
#
# 详情是**组装**而非"查一张表"：画像、单据、特征、图谱、证据分散在 rc_ 与
# biz_ 六张表里。把这些查询从接口层收拢到服务模块的理由与 case_service 本
# 身一致 —— 接口层只做参数与权限，取数口径集中在服务层，测试可以不起
# FastAPI 直接断言。
# --------------------------------------------------------------------------- #
def _iso(value: Any) -> str | None:
    """datetime -> ISO8601 字符串；其余值直返（宽松输出给前端）。"""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def case_to_dict(case: RcCase) -> dict[str, Any]:
    """案件行的字典化（列表与详情共用，避免两处字段漂移）。"""
    return {
        "case_no": case.case_no,
        "subject_type": case.subject_type,
        "subject_value": case.subject_value,
        "scene": case.scene,
        "status": case.status,
        "risk_level": case.risk_level,
        "max_score": case.max_score,
        "hit_cnt": case.hit_cnt,
        "event_cnt": case.event_cnt,
        "first_at": _iso(case.first_at),
        "last_at": _iso(case.last_at),
        "handler": case.handler,
        "handler_id": case.handler_id,
        "claimed_at": _iso(case.claimed_at),
        "dispose_result": case.dispose_result,
        "disposed_at": _iso(case.disposed_at),
        "archived_at": _iso(case.archived_at),
        "closed_at": _iso(case.closed_at),
        "close_reason": case.close_reason,
        "created_at": _iso(case.created_at),
    }


def case_event_to_dict(item: RcCaseEvent) -> dict[str, Any]:
    """案件事件关联行的字典化（时间线）。"""
    return {
        "event_id": item.event_id,
        "decision_id": item.decision_id,
        "event_type": item.event_type,
        "scene": item.scene,
        "action": item.action,
        "risk_level": item.risk_level,
        "risk_score": item.risk_score,
        "hit_count": item.hit_count,
        "biz_no": item.biz_no,
        "occurred_at": _iso(item.occurred_at),
    }


def build_case_detail(db: Session, case: RcCase) -> dict[str, Any]:
    """组装工作台详情（画像 + 单据 + 特征 + 图谱 + 证据）。

    **取数基准是案件里"最近一次事件"**（``last_event_id``）：工作台默认
    展开的就是这一条决策的证据；更早的关联事件交给时间线去翻，而不是在
    这里做聚合。
    """
    subject_value = case.subject_value
    last_event_id = case.last_event_id
    last_decision_id = case.last_decision_id

    # ---- 画像 / 单据 / 特征 / 图谱按主体聚合 ----
    profile = _profile_payload(db, subject_value)
    graph = _graph_payload(db, subject_value)
    biz_doc = _biz_doc_payload(db, case, subject_value)

    events = list_case_events(db, case.case_no)
    decisions = _decisions_payload(db, events)

    focus = _focus_evidence(db, event_id=last_event_id, decision_id=last_decision_id)

    return {
        "case": case_to_dict(case),
        "events": [case_event_to_dict(item) for item in events],
        "decisions": decisions,
        "profile": profile,
        "biz_doc": biz_doc,
        "graph": graph,
        "focus": focus,
    }


def _decisions_payload(db: Session, events: list[RcCaseEvent]) -> list[dict[str, Any]]:
    """案件内各事件的决策摘要（时间线面板用，不展开整张决策）。"""
    event_ids = [event.event_id for event in events]
    if not event_ids:
        return []
    rows = db.execute(
        select(RcDecision).where(RcDecision.event_id.in_(event_ids))
    ).scalars().all()
    by_event = {row.event_id: row for row in rows}
    payload: list[dict[str, Any]] = []
    for event in events:
        decision = by_event.get(event.event_id)
        payload.append(
            {
                "event_id": event.event_id,
                "decision_id": event.decision_id,
                "action": event.action,
                "risk_level": event.risk_level,
                "risk_score": event.risk_score,
                "rule_score": decision.rule_score if decision else None,
                "model_score": decision.model_score if decision else None,
                "model_version": decision.model_version if decision else None,
            }
        )
    return payload


def _focus_evidence(db: Session, *, event_id: str | None, decision_id: str | None) -> dict[str, Any]:
    """最近一次决策的完整证据块：三分数 + 命中规则 + 模型贡献 + 特征 + 事件上下文。

    按 rc_decision 取而不是按 event_id 取其 event：重复投递的幂等回放路径
    只保证 event_id 一致，决策行可能来自另一事务（本系统为同事务，两者
    等价，但按 decision_id 查更不容易将来错乱）。
    """
    decision = None
    if decision_id:
        decision = db.execute(
            select(RcDecision).where(RcDecision.decision_id == decision_id)
        ).scalar_one_or_none()
    if decision is None and event_id:
        decision = db.execute(
            select(RcDecision).where(RcDecision.event_id == event_id)
        ).scalar_one_or_none()

    event = None
    if event_id:
        event = db.execute(select(RcEvent).where(RcEvent.event_id == event_id)).scalar_one_or_none()
    if event is None and decision is not None:
        event = db.execute(select(RcEvent).where(RcEvent.event_id == decision.event_id)).scalar_one_or_none()

    if decision is None:
        return {"decision": None, "hit_rules": [], "model_contributions": [], "features": [], "event_context": {}}

    hit_rows = list(
        db.execute(
            select(RcDecisionHit).where(RcDecisionHit.decision_id == decision.decision_id)
            .order_by(RcDecisionHit.score.desc(), RcDecisionHit.id.asc())
        ).scalars().all()
    )
    contribution_rows = list(
        db.execute(
            select(RcModelContribution)
            .where(RcModelContribution.decision_id == decision.decision_id)
            .order_by(RcModelContribution.rank_no.asc())
        ).scalars().all()
    )
    snapshot_row = None
    if event is not None:
        snapshot_row = db.execute(
            select(RcFeatureSnapshot).where(RcFeatureSnapshot.event_id == event.event_id)
        ).scalar_one_or_none()

    return {
        "decision": {
            "decision_id": decision.decision_id,
            "event_id": decision.event_id,
            "rule_score": decision.rule_score,
            "model_score": decision.model_score,
            "risk_score": decision.risk_score,
            "risk_level": decision.risk_level,
            "action": decision.action,
            "action_hint": decision.action_hint,
            "decided_by": decision.decided_by,
            "model_version": decision.model_version,
            "hit_count": decision.hit_count,
        },
        "hit_rules": [
            {
                "rule_code": row.rule_code,
                "rule_name": row.rule_name,
                "rule_category": row.rule_category,
                "score": row.score,
                "reason": row.reason,
                "evidence": row.evidence or [],
            }
            for row in hit_rows
        ],
        "model_contributions": [
            {
                "feature_name": row.feature_name,
                "feature_value": float(row.feature_value) if row.feature_value is not None else None,
                "contribution": float(row.contribution),
                "direction": row.direction,
                "rank_no": row.rank_no,
            }
            for row in contribution_rows
        ],
        "features": _feature_groups(snapshot_row.features if snapshot_row else {}),
        "event_context": _event_context(event, snapshot_row),
        "feature_version": snapshot_row.feature_version if snapshot_row else None,
        "window_profile": snapshot_row.window_profile if snapshot_row else None,
    }


def _feature_groups(features: dict[str, Any]) -> list[dict[str, Any]]:
    """把扁平特征字典按 agg 类型分组，供工作台分组表格展示。

    分组键用 ``agg`` 而不是"按规则场景分"是因为工作台要回答的是
    "频次涨了吗 / 金额变了吗 / 名单命中了吗"，这正好对应聚合方式的
    语义；按场景分会把"同一场景内的频次与金额"拆到两个卡片，反而
    打断了"先看频次再看金额"的自然顺序。
    """
    if not features:
        return []
    from app.services.feature_engine import FEATURE_SPECS_BY_KEY, is_feature_key

    agg_fallback = "其他"
    groups: dict[str, list[dict[str, Any]]] = {}
    for key, value in features.items():
        if not is_feature_key(key):
            continue
        base = key.rsplit("_", 1)[0] if key.rsplit("_", 1)[-1] in {"1h", "24h", "7d"} else key
        spec = FEATURE_SPECS_BY_KEY.get(base)
        group_name = {
            "count": "频次统计",
            "sum": "金额统计",
            "distinct": "聚集度",
            "ratio": "比例特征",
            "flag": "名单与标记",
            "profile": "主体画像",
        }.get(spec.agg if spec else None, agg_fallback)
        description = spec.description if spec else ""
        groups.setdefault(group_name, []).append(
            {"key": key, "value": value, "description": description}
        )
    # 组内按键名排序，前端不依赖字典序的稳定输出
    return [
        {"group": name, "items": sorted(items, key=lambda item: item["key"])}
        for name, items in groups.items()
    ]


def _event_context(event: RcEvent | None, snapshot_row: RcFeatureSnapshot | None) -> dict[str, Any]:
    """事件上下文（工作台证据区的"当时这条事件是什么样"）。

    取**落库事实**而不是从 payload 重建上下文：``.device_fingerprint`` 与
    ``.ip_region`` 是入库时从信拆解出来的列，按列回显不会受"当时 payload
    里有没有这个字段"影响，口径稳定。
    """
    if event is None:
        return {}
    return {
        "event": {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "user_id": event.user_id,
            "phone": event.phone,
            "device_id": event.device_id,
            "device_fingerprint": dict(event.device_fingerprint or {}),
            "ip": event.ip,
            "ip_region": event.ip_region,
            "address_hash": event.address_hash,
            "biz_no": event.biz_no,
            "occurred_at": _iso(event.occurred_at),
            "source": event.source,
        },
        "payload": dict(event.payload or {}),
    }


def _profile_payload(db: Session, subject_value: str) -> dict[str, Any]:
    """用户画像卡（docs/PRD.md §11.3：账号年龄 / 名单状态 / 历史案件数 / 行为概览）。"""
    customer = db.execute(
        select(BizCustomer).where(BizCustomer.user_id == subject_value)
    ).scalar_one_or_none()
    account_age_days: int | None = None
    if customer and customer.register_at:
        account_age_days = max(0, int((utcnow() - customer.register_at).total_seconds() // 86400))

    now = utcnow()
    # 画像卡只回答"**这个账号**在不在名单里"，因此按 value 精确匹配。
    # 因设备/地址关联命中的名单不在这一栏（那是图谱节点与决策证据的职责）：
    # 把"该账号被拉黑"与"它用过的某台设备被拉黑"混在一张卡片里，
    # 审核员会误以为账号本身已处置。过期条目按"未命中"过滤但不删除（审计证据）。
    list_rows = db.execute(
        select(RcListEntry).where(
            RcListEntry.value == subject_value,
            RcListEntry.status == LIST_STATUS_ACTIVE,
            or_(RcListEntry.expire_at.is_(None), RcListEntry.expire_at > now),
        )
    ).scalars().all()
    list_status: list[dict[str, Any]] = [
        {
            "list_type": row.list_type,
            "dimension": row.dimension,
            "reason": row.reason,
            "expire_at": _iso(row.expire_at),
        }
        for row in list_rows
    ]

    behavior: dict[str, int] = {}
    since_7d = now - timedelta(days=7)
    behavior["event_cnt_7d"] = int(
        db.execute(
            select(func.count()).select_from(RcEvent).where(
                RcEvent.user_id == subject_value, RcEvent.occurred_at >= since_7d
            )
        ).scalar_one()
        or 0
    )
    behavior["case_cnt_30d"] = int(
        db.execute(
            select(func.count()).select_from(RcCase).where(
                RcCase.subject_value == subject_value,
                RcCase.created_at >= now - timedelta(days=30),
            )
        ).scalar_one()
        or 0
    )

    return {
        "subject_value": subject_value,
        "customer": (
            {
                "user_id": customer.user_id,
                "phone": customer.phone,
                "level": customer.level,
                "status": customer.status,
                "register_channel": customer.register_channel,
                "register_at": _iso(customer.register_at),
            }
            if customer
            else None
        ),
        "account_age_days": account_age_days,
        "list_status": list_status,
        "behavior": behavior,
    }


def _biz_doc_payload(db: Session, case: RcCase, subject_value: str) -> dict[str, Any] | None:
    """当前业务单据卡：订单 / 退款。

    取的是**案件关联事件的 biz_no 对应的真实单据**而不是"主体名下最新一条"，
    两者在"同一主体存在多个订单"时结果不同；这里必须展示**被处置**的那一张。
    """
    events = list_case_events(db, case.case_no)
    if case.scene == "order":
        order_no = next(
            (event.biz_no for event in events if event.biz_no), None
        )
        if not order_no:
            return None
        order = db.execute(select(BizOrder).where(BizOrder.order_no == order_no)).scalar_one_or_none()
        if order is None:
            return {"kind": "order", "order_no": order_no, "missing": True}
        return {
            "kind": "order",
            "order_no": order.order_no,
            "user_id": order.user_id,
            "product_id": order.product_id,
            "quantity": order.quantity,
            "amount": float(order.amount),
            "status": order.status,
            "missing": False,
        }
    if case.scene == "after_sale":
        refund_no = next(
            (event.biz_no for event in events if event.event_type == "after_sale_apply" and event.biz_no),
            None,
        )
        if not refund_no:
            return None
        refund = db.execute(
            select(BizRefund).where(BizRefund.refund_no == refund_no)
        ).scalar_one_or_none()
        if refund is None:
            return {"kind": "refund", "refund_no": refund_no, "missing": True}
        # 关联订单：让审核员一眼看到"这单退款对应的是哪个订单的多少钱"
        order = db.execute(
            select(BizOrder).where(BizOrder.order_no == refund.order_no)
        ).scalar_one_or_none()
        return {
            "kind": "refund",
            "refund_no": refund.refund_no,
            "order_no": refund.order_no,
            "user_id": refund.user_id,
            "refund_amount": float(refund.refund_amount),
            "reason": refund.reason,
            "status": refund.status,
            "missing": False,
            "order": (
                {
                    "order_no": order.order_no,
                    "product_id": order.product_id,
                    "quantity": order.quantity,
                    "amount": float(order.amount),
                    "status": order.status,
                }
                if order
                else None
            ),
        }
    # login / coupon 场景没有"业务单据卡"：这两个场景的证据由画像 + 特征 + 图谱承担
    return None


def _graph_payload(db: Session, subject_value: str) -> dict[str, Any]:
    """关联实体图谱（中心 = 主体；邻居 = 同设备 / 同 IP / 同地址 / 同手机号的其它账号）。

    实现口径**用事件表回查**（同实体的并集），这是"图谱"该回答的问题。
    用一个不大不小的窗口（24 小时）截断：同一 IP 下 3 天前出现的咖啡馆账号
    与"此刻与案件主体共设备"的账号是两类证据，不该混在一起。

    ``LIMIT`` 是硬上限：脏数据或攻击面可能让某个 IP 下有几百个账号，
    无上限的查询会把"看一个案件"变成一次扫表。
    """
    since = utcnow() - timedelta(hours=24)
    subjects = _subjects_of(db, subject_value, since)
    neighbors: dict[str, set[str]] = {}
    max_neighbors = 30
    for label, value in subjects:
        if not value:
            continue
        others = _same_entity_users(db, label, value, subject_value, since, limit=max_neighbors)
        for other in others:
            neighbors.setdefault(other, set()).add(label)

    # 节点与边去重：同一"邻居"可能同时满足"同设备"与"同 IP"两类关系，
    # 每条关系在 ECharts 力导向图里是一条独立边，但节点只能有一个。
    neighbor_cases = _case_counts(db, list(neighbors.keys()) or [subject_value])
    nodes = [
        {
            "id": subject_value,
            "label": subject_value,
            "risk_level": _risk_of(db, subject_value),
            "case_cnt": _case_counts(db, [subject_value]).get(subject_value, 0),
            "is_center": True,
            "size": 28,
        }
    ]
    edges: list[dict[str, Any]] = []
    for neighbor in sorted(neighbors.keys()):
        labels = sorted(neighbors[neighbor])
        nodes.append(
            {
                "id": neighbor,
                "label": neighbor,
                "risk_level": _risk_of(db, neighbor),
                "case_cnt": neighbor_cases.get(neighbor, 0),
                "is_center": False,
                "size": min(24, 12 + 4 * len(labels)),
            }
        )
        for label in labels:
            edges.append({"source": subject_value, "target": neighbor, "label": label})
    return {"nodes": nodes, "edges": edges}


def _subjects_of(db: Session, subject_value: str, since: datetime) -> list[tuple[str, str | None]]:
    """取主体最近一次事件的实体标识（device / ip / phone / address_hash）。"""
    row = db.execute(
        select(RcEvent.device_id, RcEvent.ip, RcEvent.phone, RcEvent.address_hash)
        .where(RcEvent.user_id == subject_value)
        .order_by(RcEvent.occurred_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return []
    device_id, ip, phone, address_hash = row
    return [
        ("device", device_id),
        ("ip", ip),
        ("phone", phone),
        ("address", address_hash),
    ]


def _same_entity_users(
    db: Session,
    label: str,
    value: str,
    subject_value: str,
    since: datetime,
    *,
    limit: int,
) -> list[str]:
    """取同一维度下出现过的其它账号（device / ip / phone / address_hash 的并集）。"""
    column = {
        "device": RcEvent.device_id,
        "ip": RcEvent.ip,
        "phone": RcEvent.phone,
        "address": RcEvent.address_hash,
    }.get(label)
    if column is None:
        return []
    rows = (
        db.execute(
            select(RcEvent.user_id)
            .where(
                column == value,
                RcEvent.user_id != subject_value,
                RcEvent.occurred_at >= since,
            )
            .distinct()
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [str(user_id) for user_id in rows if user_id]


def _case_counts(db: Session, subject_values: list[str]) -> dict[str, int]:
    """每个主体的案件数（图谱节点展示用，近 30 天）。"""
    if not subject_values:
        return {}
    rows = db.execute(
        select(RcCase.subject_value, func.count())
        .where(RcCase.subject_value.in_(subject_values), RcCase.created_at >= utcnow() - timedelta(days=30))
        .group_by(RcCase.subject_value)
    ).all()
    return {str(subject): int(count) for subject, count in rows}


def _risk_of(db: Session, subject_value: str) -> str:
    """主体的**未结案件**风险等级（图谱节点颜色）。

    没有未结案件时用 low：历史案件已处置的风险不应污染"这个账号现在
    是否还需要关注"的判断。
    """
    row = db.execute(
        select(RcCase.risk_level)
        .where(RcCase.subject_value == subject_value, RcCase.status.in_(OPEN_STATUSES))
        .order_by(RcCase.last_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row if row else "low"
