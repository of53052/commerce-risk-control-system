"""名单库模型（黑 / 白 / 灰 × 五维度）。

设计要点：
- 唯一键 (list_type, dimension, value)：同一实体在同一名单类型下只应有一条有效记录，
  重复导入时应走"更新而非新增"，否则名单会越滚越大且语义冲突。
- priority 字段支撑"黑白同时命中怎么办"的可配置语义（默认黑名单优先）。
- 过期用 expire_at 逻辑判断而非物理删除：名单过期仍是审计证据。
"""

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

LIST_BLACK = "black"
LIST_WHITE = "white"
LIST_GRAY = "gray"

DIM_USER = "user"
DIM_PHONE = "phone"
DIM_IP = "ip"
DIM_DEVICE = "device"
DIM_ADDRESS = "address"

ALL_DIMENSIONS = (DIM_USER, DIM_PHONE, DIM_IP, DIM_DEVICE, DIM_ADDRESS)

SOURCE_MANUAL = "manual"
SOURCE_IMPORT = "import"
SOURCE_AUTO = "auto"

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"


class RcListEntry(Base):
    """名单条目。"""

    __tablename__ = "rc_list_entry"
    __table_args__ = (
        Index("uq_rc_list_type_dim_value", "list_type", "dimension", "value", unique=True),
        Index("ix_rc_list_dim_value", "dimension", "value"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    list_type: Mapped[str] = mapped_column(String(8), nullable=False, comment="black/white/gray")
    dimension: Mapped[str] = mapped_column(String(16), nullable=False, comment="user/phone/ip/device/address")
    value: Mapped[str] = mapped_column(String(128), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="数值越小优先级越高")
    reason: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_MANUAL, nullable=False)
    expire_at: Mapped[object | None] = mapped_column(DateTime, comment="为空表示永久有效")
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
