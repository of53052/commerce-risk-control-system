"""模拟业务端：执行真实业务动作，并在动作前后调用风控网关。

**"前后都调用"是刻意的**（PRD §6.2 原文）：业务动作前的调用是**前置决策**
（Reject 则不执行），动作后……在 P0 里不做二次调用 —— 事件本身就是动作的
输入，动作的落库结果通过后续事件（如支付、退款）再回到风控。
这里保留"前置决策"这一层，并把每次调用的决策结果**完整返回给调用方**，
便于演示脚本逐条打印链路。

放行规则只认 ``Reject``：

* ``Pass`` / ``Challenge`` / ``Review``：业务动作照常执行（Challenge 的二次验证、
  Review 的人工审核发生在处置阶段，见 PRD §9）；
* ``Reject``：**不执行**业务动作，且不落业务表。

这条规则必须写死在业务侧、而不是"看分数决定"：分数是风控的内部计量，
业务系统只应消费 ``action`` 这一层契约 —— 否则风控改一次阈值标定，
业务侧的拦截行为就会跟着变，两边再也对不上账。
"""

from __future__ import annotations

import itertools
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.biz import (
    CUSTOMER_NORMAL,
    ORDER_CREATED,
    ORDER_PAID,
    REFUND_APPLIED,
    BizCouponReceive,
    BizCustomer,
    BizOrder,
    BizRefund,
)
from app.models.decision import ACTION_REJECT
from app.schemas.event import EventIn
from app.services import event_gateway
from app.simulator.actors import Actor

logger = logging.getLogger("app.simulator.service")

# --------------------------------------------------------------------------- #
# 单号生成的进程级状态
# --------------------------------------------------------------------------- #
# 模拟端要生成的"号"有两类：业务单号（订单号/退款单号/券号）与事件号（幂等键）。
# 两类都必须**全局唯一**，但它们的唯一性来源不同：业务单号撞了会被数据库唯一约束
# 直接拒绝，事件号撞了则更隐蔽 —— 后来者会被幂等缓存"回放"成前一条事件的决策，
# 整条事件静默丢失。因此这里用三段拼装来保证唯一：
#
#   前缀 + 事件发生时刻(YYMMDDHHMMSS) + 进程盐 + 进程内全局自增序号
#
# 之所以**不用"秒级时间戳 + 实例自增"**：场景脚本给每个场景新建一个
# ``BusinessSimulator``，同一秒内两个实例都从 1 开始计数，就会生成完全相同的号。
# 序号抬到模块级（跨实例共享）解决实例间撞号；进程盐解决进程间撞号。
#
# **进程盐不能用 PID**：事件号里的时间戳取的是事件自身的 ``occurred_at``（可能是
# 6 天前），不是墙上时钟，所以"同一秒 + 同一序号"在重跑时会天然对齐；此时只要
# 操作系统复用了同一个 PID（Windows 上很常见），两次运行就会生成一模一样的事件号，
# 第二批事件会被幂等缓存当成重复投递而整批丢弃。用随机盐把这条路彻底堵死：
# 撞号需要"同一事件时刻 + 同一随机盐"，概率可忽略。
_NO_SALT = secrets.token_hex(4).upper()
_NO_COUNTER = itertools.count(1)


@dataclass
class BusinessResult:
    """一次业务动作的结果：是否放行 + 风控决策 + 业务单据号。

    ``biz_no`` 的语义严格限定为**已落库的业务单据号**：被拒时它是 ``None``，
    而不是"原本会生成的单号"。这两者在演示与排障里天差地别 ——
    打印出单号会让人以为券发出去了、订单建好了，而业务表里根本没有这行数据。
    """

    allowed: bool
    action: str
    biz_no: str | None
    decision: dict[str, Any]
    reason: str = ""


class BusinessSimulator:
    """把 `Actor` 的业务动作翻译成事件 + 业务单据。

    每个方法都遵循同一套三步：**构造事件 → 交给网关 → 按 action 决定是否落业务表**。
    这样新增一类业务动作的成本固定，不会出现"某类动作忘记调风控"的漏洞
    （漏调风控的表现是"这个动作永远不被拦"，从日志上看不出来）。
    """

    def __init__(self, db: Session, *, source: str = "simulation", autocommit: bool = True) -> None:
        """``autocommit=False`` 时由调用方掌管事务（数据集生成器需要批量提交）。

        为什么需要它：数据集生成要跑几万条事件，逐条 commit 会把吞吐压在
        40 事件/秒上下；批量提交把事务次数降到 1/100。这里不引入"每 N 条自动提交"
        之类的隐式行为 —— 提交时机是调用方的策略，模拟端只负责不再擅自提交。
        """
        self.db = db
        self.source = source
        self.autocommit = autocommit

    # ------------------------------------------------------------------ #
    # 业务动作
    # ------------------------------------------------------------------ #
    def register(self, actor: Actor, *, occurred_at=None) -> BizCustomer:
        """注册账号（不产生风控事件：账号注册本身不是五类风控事件之一）。

        注册时间会被特征引擎读取（``account_age_days``），因此 ``occurred_at``
        是**画像开关**：省略 = 新注册账号（命中"新账号高额订单"这类规则）；
        传 60 天前 = 老账号（用于演示"环境干净、只靠行为序列识别"的场景）。

        注意它与其他方法的时间语义不同 —— 这里构造的是"注册那一刻"，
        而不是"业务动作发生的那一刻"，所以签名与 ``login`` 等保持一致，
        但调用方的意图是"这个账号是什么时候注册的"。
        """
        existed = self.db.execute(select(BizCustomer).where(BizCustomer.user_id == actor.user_id)).scalar_one_or_none()
        if existed is not None:
            return existed
        customer = BizCustomer(
            user_id=actor.user_id,
            phone=actor.phone,
            register_at=occurred_at or utcnow(),
            register_channel="simulator",
            status=CUSTOMER_NORMAL,
        )
        self.db.add(customer)
        self._flush_or_commit()
        return customer

    def login(self, actor: Actor, *, occurred_at=None) -> BusinessResult:
        """登录（风控可拒）。"""
        event = self._event(
            actor,
            event_type="login",
            occurred_at=occurred_at,
            payload={"login_type": "password", "login_result": "success"},
        )
        return self._decide(event, biz_no=None)

    def receive_coupon(
        self,
        actor: Actor,
        *,
        coupon_id: str = "CP-BIG-150",
        coupon_name: str = "大促满减券",
        face_value: float = 150.0,
        channel: str = "app",
        occurred_at=None,
    ) -> BusinessResult:
        """领券（风控可拒）。被拒时不落 biz_coupon_receive —— 券没发出去。"""
        moment = occurred_at or utcnow()
        receive_no = self._next_no("RC", occurred_at=moment)
        event = self._event(
            actor,
            event_type="coupon_receive",
            occurred_at=moment,
            payload={
                "receive_no": receive_no,
                "coupon_id": coupon_id,
                "coupon_name": coupon_name,
                "face_value": face_value,
                "channel": channel,
            },
        )
        result = self._decide(event, biz_no=receive_no)
        if result.allowed:
            self.db.add(
                BizCouponReceive(
                    receive_no=receive_no,
                    user_id=actor.user_id,
                    coupon_id=coupon_id,
                    face_value=face_value,
                    channel=channel,
                )
            )
            self._flush_or_commit()
        return result

    def create_order(
        self,
        actor: Actor,
        *,
        product_id: str = "P-1001",
        quantity: int = 1,
        amount: float = 199.0,
        pay_method: str = "balance",
        occurred_at=None,
    ) -> BusinessResult:
        """下单（风控可拒）。"""
        moment = occurred_at or utcnow()
        order_no = self._next_no("SO", occurred_at=moment)
        event = self._event(
            actor,
            event_type="order_create",
            occurred_at=moment,
            payload={
                "order_no": order_no,
                "product_id": product_id,
                "quantity": quantity,
                "amount": amount,
                "pay_method": pay_method,
            },
        )
        result = self._decide(event, biz_no=order_no)
        if result.allowed:
            self.db.add(
                BizOrder(
                    order_no=order_no,
                    user_id=actor.user_id,
                    product_id=product_id,
                    quantity=quantity,
                    amount=amount,
                    status=ORDER_CREATED,
                    address_hash=actor.address.address_hash if actor.address else None,
                )
            )
            self._flush_or_commit()
        return result

    def pay_order(self, actor: Actor, *, order_no: str, occurred_at=None) -> BusinessResult:
        """支付（风控可拒）。

        订单必须先存在：事件校验器会拒绝"支付不存在的订单"（见 event_validator），
        这保证了业务序列的因果一致 —— 演示脚本不会因为漏了 compatible 的下单步骤
        而写出"凭空支付的订单"。
        """
        order = self.db.execute(select(BizOrder).where(BizOrder.order_no == order_no)).scalar_one_or_none()
        if order is None:
            raise ValueError(f"订单不存在，无法支付：{order_no}（请先调用 create_order）")

        event = self._event(
            actor,
            event_type="order_pay",
            occurred_at=occurred_at,
            payload={
                "order_no": order_no,
                "pay_amount": float(order.amount),
                "pay_channel": "balance",
                "pay_status": "success",
            },
        )
        result = self._decide(event, biz_no=order_no)
        if result.allowed:
            order.status = ORDER_PAID
            self._flush_or_commit()
        return result

    def apply_refund(
        self,
        actor: Actor,
        *,
        order_no: str,
        refund_amount: float | None = None,
        reason: str = "未收到货",
        apply_type: str = "refund_only",
        occurred_at=None,
    ) -> BusinessResult:
        """申请退款（风控可拒）。被拒时退款单不落库，业务上等同"申请未受理"。"""
        moment = occurred_at or utcnow()
        refund_no = self._next_no("RF", occurred_at=moment)
        order = self.db.execute(select(BizOrder).where(BizOrder.order_no == order_no)).scalar_one_or_none()
        amount = refund_amount if refund_amount is not None else float(order.amount) if order else 0.0
        event = self._event(
            actor,
            event_type="after_sale_apply",
            occurred_at=moment,
            payload={
                "refund_no": refund_no,
                "order_no": order_no,
                "refund_amount": amount,
                "reason": reason,
                "apply_type": apply_type,
            },
        )
        result = self._decide(event, biz_no=refund_no)
        if result.allowed:
            self.db.add(
                BizRefund(
                    refund_no=refund_no,
                    order_no=order_no,
                    user_id=actor.user_id,
                    refund_amount=amount,
                    reason=reason,
                    apply_type=apply_type,
                    status=REFUND_APPLIED,
                )
            )
            self._flush_or_commit()
        return result

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _decide(self, event: EventIn, *, biz_no: str | None) -> BusinessResult:
        """交给风控网关，并把 action 翻译成"业务是否放行"。"""
        # commit 交给 _flush_or_commit()：批量模式下由调用方统一提交，
        # 否则"网关提交了、业务单据还没提交"会留下不一致窗口。
        outcome = event_gateway.handle_event(self.db, event=event, commit=self.autocommit)
        decision = outcome.response
        action = str(decision.get("action") or "")
        allowed = action != ACTION_REJECT
        reason = ""
        if not allowed:
            reason = "风控拒绝，业务动作未执行"
        elif action != "Pass":
            reason = f"风控放行但转人工/验证（{action}），业务动作已执行"
        return BusinessResult(
            allowed=allowed,
            action=action,
            biz_no=biz_no if allowed else None,
            decision=decision,
            reason=reason,
        )

    def _flush_or_commit(self) -> None:
        """按 ``autocommit`` 决定"立即提交"还是"只 flush"（见 ``__init__``）。

        名字刻意带上 ``flush``：单文件模式下它是 commit，批量模式下它只把
        待写行推给数据库（可回滚），叫 ``_commit`` 会让人误以为"调用即落盘"，
        进而把"批量中途异常 = 整批回滚"的语义读错。
        """
        if self.autocommit:
            self.db.commit()
        else:
            # 批量模式下也要**让本会话能看到刚写入的行**：后续动作（支付/退款）
            # 需要按单号查订单，只有 flush 之后 SQL 查询才能命中未提交的数据。
            # 顺带把主键填上，避免模型对象处于"半初始化"状态。
            self.db.flush()

    def _event(
        self,
        actor: Actor,
        *,
        event_type: str,
        payload: dict[str, Any],
        occurred_at=None,
    ) -> EventIn:
        """组装事件（信封 + 专有字段），字段口径见 docs/PRD.md §6.1。"""
        moment = occurred_at or utcnow()
        address = None
        if actor.address is not None:
            address = {
                "address_hash": actor.address.address_hash,
                "region": actor.address.region,
                "receiver_name": actor.address.receiver_name,
                "phone": actor.address.phone or actor.phone,
            }
        return EventIn(
            event_id=self._next_no("EVT", occurred_at=moment),
            event_type=event_type,
            occurred_at=moment,
            user_id=actor.user_id,
            phone=actor.phone,
            device={"device_id": actor.device.device_id, "fingerprint": dict(actor.device.fingerprint)},
            network={
                "ip": actor.network.ip,
                "ip_region": actor.network.ip_region,
                "is_proxy": actor.network.is_proxy,
            },
            address=address,
            payload=payload,
            source=self.source,
        )

    def _next_no(self, prefix: str, *, occurred_at=None) -> str:
        """生成业务号/事件号：前缀 + 事件时刻 + 进程盐 + 全局序号。

        两个刻意的选择：

        * **时间戳取事件自身的 ``occurred_at`` 而不是墙上时钟**。演示会把事件
          回放到"6 天前"，若号里写的是当前时刻，日志与库里的时间会对不上，
          人工核对时反而增加判断成本。
        * **序号来自模块级自增**（见文件顶部 ``_NO_COUNTER``）：同一次演示里
          单号仍然递增可读，同时保证跨实例、跨场景不重复。
        """
        moment = occurred_at or utcnow()
        return f"{prefix}{moment.strftime('%y%m%d%H%M%S')}{_NO_SALT}{next(_NO_COUNTER):04d}"
