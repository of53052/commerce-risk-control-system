"""审计服务：规范化 JSON、SHA-256 前向哈希链、链校验。

链式结构（docs/ARCHITECTURE.md §7.7）::

    prev   = 上一条的 hash（第一条为 GENESIS = 64 个 0）
    payload = canonical_json({actor_id, actor_name, role, action, target_type,
                              target_id, before_json, after_json, reason, created_at})
    hash    = sha256(prev + payload)

**规范化的三件事不是洁癖，而是哈希稳定的前提**：
    1. 键按字典序排序 —— 否则 dict 插入顺序变化就会算出不同哈希；
    2. 时间统一 ISO8601（秒级、UTC、无时区后缀）—— 同一时刻的不同表示形式
       会产生不同字符串；
    3. 空值统一写 ``null`` 而不是省略键 —— "键不存在"与"值为空"在业务上
       可能是两件事，但对哈希来说必须只有一种表示。
    做不到这三点，链会在"内容没变但序列化变了"的时候假断裂，
    然后所有人开始怀疑校验器，而不是怀疑数据。

**串行化**：``write`` 通过模块级锁 + 同一事务内"读上一条 hash → 写新记录"
保证单进程内严格有序。多进程写入需要 DB 行锁或分布式锁，属于 P2 之后的扩展点
（docs/ARCHITECTURE.md §16）。单实例部署下当前实现足够。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.audit import GENESIS_HASH, RcAuditLog

logger = logging.getLogger("app.services.audit_service")

# 参与哈希的业务字段（顺序无关，规范化时会排序）
HASH_FIELDS = (
    "actor_id",
    "actor_name",
    "role",
    "action",
    "target_type",
    "target_id",
    "before_json",
    "after_json",
    "reason",
    "created_at",
)

# 写链锁：保证"读上一条 hash → 写入新记录"这一对操作不被并发穿插
_write_lock = threading.Lock()


@dataclass
class VerifyReport:
    """链校验报告（对应 GET /api/v1/audit/verify 的返回）。"""

    total: int
    valid: bool
    first_broken_id: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "valid": self.valid,
            "first_broken_id": self.first_broken_id,
            "detail": self.detail,
        }


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空白、非 ASCII 不转义。

    ``ensure_ascii=False`` 很关键：审计内容里有中文（处置原因、规则名），
    转义成 ``\\uXXXX`` 虽然也能算哈希，但人读审计流水时无法直接看懂，
    而审计的第一价值是"人能读"。
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def _default(value: Any) -> Any:
    """json 序列化的兜底：datetime 转 ISO8601（秒级、naive、无时区后缀）。

    只保留秒级是刻意的：``utcnow()`` 本来就是秒级精度，
    而如果允许微秒进入哈希，同一"业务时刻"在不同代码路径下可能带不同微秒，
    链就会因为无关紧要的精度差异假断裂。
    """
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value)


def _payload_of(row: RcAuditLog) -> str:
    """从记录取出参与哈希的业务字段并规范化。"""
    data = {field: getattr(row, field) for field in HASH_FIELDS}
    return canonical_json(data)


def compute_hash(prev_hash: str, row: RcAuditLog) -> str:
    """计算一条记录的哈希。"""
    return hashlib.sha256((prev_hash + _payload_of(row)).encode("utf-8")).hexdigest()


def last_hash(db: Session) -> str:
    """取链尾哈希；空链返回 GENESIS。"""
    row = db.execute(
        select(RcAuditLog).order_by(RcAuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    return row.hash if row is not None else GENESIS_HASH


def write(
    db: Session,
    *,
    action: str,
    actor_id: str | None = None,
    actor_name: str | None = None,
    role: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
    created_at: datetime | None = None,
    commit: bool = False,
) -> RcAuditLog:
    """追加一条审计记录。

    ``before`` / ``after`` 传 Python 对象（dict 等）即可，这里统一规范化成字符串
    再入库 —— 调用方不需要关心"要不要先 dumps"，避免各处写出一堆格式不一致的 JSON。

    ``commit=False``（默认）把提交留给调用方：审计与业务变更应当在**同一事务**里，
    否则会出现"业务改成功但审计没写进去"的窗口，而审计的意义正是消除这种窗口。
    """
    with _write_lock:
        prev = last_hash(db)
        row = RcAuditLog(
            actor_id=actor_id,
            actor_name=actor_name,
            role=role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_json=None if before is None else canonical_json(before),
            after_json=None if after is None else canonical_json(after),
            reason=reason,
            prev_hash=prev,
            hash="",
            created_at=created_at or utcnow(),
        )
        row.hash = compute_hash(prev, row)
        db.add(row)
        db.flush()
        if commit:
            db.commit()
        return row


def verify(db: Session, *, limit: int | None = None) -> VerifyReport:
    """从 GENESIS 顺序重算整条链，遇首个不匹配即返回。

    返回 ``first_broken_id`` 而不是"整体是否通过"：
    排查时需要知道**从哪一条开始断的**，那一条之后的记录都不可信。

    ``populate_existing=True`` 不是可选项：SQLAlchemy 的 identity map 会把
    "本会话已加载过的对象"直接返回，**不刷新列值**。校验审计链的场景恰恰是
    "库里刚被别的连接改过"，此时用缓存里的旧值重算哈希必然算出"链是好的"，
    从而漏报篡改 —— 这是本功能最致命的一类假阴性。
    """
    statement = select(RcAuditLog).order_by(RcAuditLog.id.asc())
    if limit:
        statement = statement.limit(limit)
    rows = db.execute(
        statement.execution_options(populate_existing=True)
    ).scalars().all()

    prev = GENESIS_HASH
    for row in rows:
        expected = compute_hash(prev, row)
        if expected != row.hash:
            detail = (
                f"第 {row.id} 条哈希不匹配：库中 {row.hash[:12]}…，"
                f"重算 {expected[:12]}…"
                + ("（prev_hash 断链）" if row.prev_hash != prev else "")
            )
            return VerifyReport(
                total=len(rows), valid=False, first_broken_id=row.id, detail=detail
            )
        if row.prev_hash != prev:
            return VerifyReport(
                total=len(rows),
                valid=False,
                first_broken_id=row.id,
                detail=f"第 {row.id} 条的 prev_hash 指向前一条的 hash 不符（链被插入或删除）",
            )
        prev = row.hash

    return VerifyReport(total=len(rows), valid=True)
