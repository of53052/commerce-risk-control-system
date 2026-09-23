"""审计模型：哈希链只增表。

三条硬约束（docs/ARCHITECTURE.md §7.7）：
1. 只允许 INSERT / SELECT：应用数据库账号不授予 UPDATE / DELETE，并加触发器兜底；
2. 每条记录的 hash = sha256(prev_hash + 规范化字段串)，prev_hash 指向上一条；
3. 写入必须串行，否则 prev_hash 会分叉，链校验必然"假断裂"。

注意：canonical JSON（键排序 + 时间统一格式 + 空值统一 null）不是洁癖，
而是哈希稳定的前提——同一业务内容若序列化结果不同，重算哈希就会对不上。
"""

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

GENESIS_HASH = "0" * 64


class RcAuditLog(Base):
    """审计日志（哈希链）。"""

    __tablename__ = "rc_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="自增即链序")
    actor_id: Mapped[str | None] = mapped_column(String(64), comment="操作者账号；系统动作为 system")
    actor_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32), nullable=False, comment="login/case_claim/case_dispose/rule_update/...")
    target_type: Mapped[str | None] = mapped_column(String(32), comment="case/rule/list/model/config")
    target_id: Mapped[str | None] = mapped_column(String(64))
    before_json: Mapped[str | None] = mapped_column(Text, comment="变更前（规范化 JSON 字符串）")
    after_json: Mapped[str | None] = mapped_column(Text, comment="变更后（规范化 JSON 字符串）")
    reason: Mapped[str | None] = mapped_column(String(255), comment="人工填写的处置/变更原因")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
