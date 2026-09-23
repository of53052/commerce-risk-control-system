"""业务域模型：模拟电商业务系统。

为什么风控系统里要有业务表？
    docs/PRD.md §9.4 要求处置动作"真实写回业务单据"（取消订单、驳回退款）。
    没有业务表，处置环节就只能"记一笔日志，假装通知了业务系统"，
    演示链路断在最后一步。

状态取值集中定义为模块常量，避免各处手写字符串导致拼写不一致。
"""

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

CUSTOMER_NORMAL = "normal"
CUSTOMER_BLACKLISTED = "blacklisted"

ORDER_CREATED = "created"
ORDER_PAID = "paid"
ORDER_CANCELLED = "cancelled"
ORDER_REFUNDED = "refunded"

REFUND_APPLIED = "applied"
REFUND_APPROVED = "approved"
REFUND_REJECTED = "rejected"


class BizCustomer(Base):
    """业务用户。"""

    __tablename__ = "biz_customer"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    register_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    register_channel: Mapped[str | None] = mapped_column(String(32))
    level: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=CUSTOMER_NORMAL, nullable=False)


class BizProduct(Base):
    """商品（含高价值商品，用于恶意退款场景）。"""

    __tablename__ = "biz_product"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    product_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)


class BizOrder(Base):
    """订单。

    status 流转：created -> paid -> (refunded) / cancelled。
    风控处置只会取消 **未支付** 的订单；已支付订单需要走退款流程，
    因此处置时对 paid 订单给出"需人工退款"提示（docs/PRD.md §9.4）。
    """

    __tablename__ = "biz_order"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    product_id: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ORDER_CREATED, nullable=False)
    address_hash: Mapped[str | None] = mapped_column(String(64), index=True, comment="收货地址指纹，用于聚集度特征")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class BizRefund(Base):
    """售后退款单。"""

    __tablename__ = "biz_refund"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    refund_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    order_no: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), comment="如：未收到货 / 质量问题")
    apply_type: Mapped[str | None] = mapped_column(String(16), comment="refund_only / return_refund")
    status: Mapped[str] = mapped_column(String(16), default=REFUND_APPLIED, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class BizCouponReceive(Base):
    """领券记录（羊毛党场景的核心留痕）。"""

    __tablename__ = "biz_coupon_receive"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    receive_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    coupon_id: Mapped[str] = mapped_column(String(32), nullable=False)
    face_value: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, comment="券面额")
    channel: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
