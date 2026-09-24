"""事件域模型：风控事件与特征快照。

关键约束：
- rc_event.event_id 唯一 —— 这是**幂等的第一道防线**（数据库兜底）。
  Redis 幂等缓存是快速路径，但它可能被清空；唯一约束保证"同一事件绝不产生两条决策"。
- rc_feature_snapshot.event_id 唯一 —— 与事件 1:1（docs/ARCHITECTURE.md §6.3）。
- source 字段区分 real / simulation：仿真事件落库可回溯，但默认不计入大盘统计
  （docs/PRD.md §11.2）。
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

EVENT_LOGIN = "login"
EVENT_COUPON_RECEIVE = "coupon_receive"
EVENT_ORDER_CREATE = "order_create"
EVENT_ORDER_PAY = "order_pay"
EVENT_AFTER_SALE_APPLY = "after_sale_apply"

ALL_EVENT_TYPES = (
    EVENT_LOGIN,
    EVENT_COUPON_RECEIVE,
    EVENT_ORDER_CREATE,
    EVENT_ORDER_PAY,
    EVENT_AFTER_SALE_APPLY,
)

SOURCE_REAL = "real"
SOURCE_SIMULATION = "simulation"


class RcEvent(Base):
    """风控事件（业务系统一次待风控的业务动作）。"""

    __tablename__ = "rc_event"
    __table_args__ = (
        # 特征计算要按实体 + 时间范围扫事件，这几个复合索引是滑动窗口回溯的基础
        Index("ix_rc_event_user_time", "user_id", "occurred_at"),
        Index("ix_rc_event_device_time", "device_id", "occurred_at"),
        Index("ix_rc_event_ip_time", "ip", "occurred_at"),
        Index("ix_rc_event_type_time", "event_type", "occurred_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False, comment="业务方生成，幂等键")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    device_id: Mapped[str | None] = mapped_column(String(64))
    device_fingerprint: Mapped[dict | None] = mapped_column(JSON, comment="os/机型/分辨率/App 版本等")
    ip: Mapped[str | None] = mapped_column(String(64))
    ip_region: Mapped[str | None] = mapped_column(String(64))
    address_hash: Mapped[str | None] = mapped_column(String(64))
    biz_no: Mapped[str | None] = mapped_column(String(32), comment="关联业务单据号（订单号/退款单号/券记录号）")
    occurred_at: Mapped[object] = mapped_column(DateTime, nullable=False, comment="业务发生时间（UTC）")
    received_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False, comment="风控接收时间（UTC）")
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_REAL, nullable=False)
    is_cheat: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="离线训练标签：1=作弊账号产生，0=正常账号产生，NULL=线上真实事件（不参与训练）",
    )
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, comment="事件类型专有字段原样留存，保证可回放")
    latency_ms: Mapped[int | None] = mapped_column(Integer, comment="接入到响应的耗时；异步回填")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class RcFeatureSnapshot(Base):
    """特征快照（一次决策对应一份）。"""

    __tablename__ = "rc_feature_snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    features: Mapped[dict] = mapped_column(JSON, nullable=False, comment="扁平键值：{feature_key_window: value}")
    window_profile: Mapped[str] = mapped_column(String(32), default="default", nullable=False)
    calc_cost_ms: Mapped[int | None] = mapped_column(Integer, comment="特征计算耗时，用于性能预算核算")
    feature_version: Mapped[str] = mapped_column(String(16), default="v1", nullable=False, comment="特征定义版本，保证历史可复现")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
