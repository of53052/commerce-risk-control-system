"""案件域模型：案件、案件事件、处置记录、处置动作明细（docs/PRD.md §9）。

四张表的分工：

| 表 | 一行代表 | 写入时机 |
| --- | --- | --- |
| ``rc_case`` | 一个待办案件 | 决策为 Review/Reject 时建案或合案 |
| ``rc_case_event`` | 案件与一次决策的关联 | 每次建案/合案 |
| ``rc_case_action`` | 一次处置（双维度结论 + 备注） | 审核员提交处置 |
| ``rc_case_action_item`` | 一项风控动作的执行结果 | 与 action 同事务 |

**为什么把 max_score / hit_cnt / event_cnt / risk_level 冗余在 rc_case**：
工作台列表页每次都要展示"最高分 / 命中次数 / 风险等级"。若每次回查
``rc_decision_hit`` 聚合，一条 SQL 就退化成"案件列表 × 命中明细"的 N+1，
而这几个值只在合案时变化一次。冗余的代价是"合案逻辑必须算对"，
收益是列表页恒定两条 SQL（count + 页数据）。

**hit_cnt 与 event_cnt 的口径必须分开记**（两者都会在合案时增长，含义不同）：
``event_cnt`` = 关联事件数（"这个人被风控盯上过几次"），
``hit_cnt``   = 累计命中规则条数（"一共踩了多少条规则"）。
合成一个字段的话，工作台就没法回答"是同一次触发踩了 3 条规则，还是触发了 3 次"。

**状态与结论用常量而非裸字符串**：状态机迁移是条件更新的 WHERE 条件
（见 app/services/case_service.py），一旦某处拼成 ``'Processing'``，
条件更新会静默影响 0 行 —— 表现为"接手没反应"，排查成本很高。
"""

from sqlalchemy import BigInteger, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

# ---- 案件状态（docs/PRD.md §9.2）----
CASE_PENDING = "pending"
CASE_PROCESSING = "processing"
CASE_DISPOSED = "disposed"
CASE_ARCHIVED = "archived"
CASE_CLOSED = "closed"

# 「未结案」状态：合案候选只在这两个状态里找。已处置/已归档/已关闭的案件
# 不允许再被合并 —— 否则"审核员刚处置完，新事件又挂到这个已处置案件上"，
# 处置结论与证据链随即脱节。
OPEN_STATUSES: tuple[str, ...] = (CASE_PENDING, CASE_PROCESSING)

ALL_STATUSES: tuple[str, ...] = (
    CASE_PENDING,
    CASE_PROCESSING,
    CASE_DISPOSED,
    CASE_ARCHIVED,
    CASE_CLOSED,
)

# ---- 主体类型（P1 只按用户建案）----
# 案件的主体固定为账号：风控处置的作用对象（拉黑、封设备、取消订单）最终都
# 通过账号的关联事件溯源，按设备/IP 建案会把多个账号的风险混进同一个案件，
# 处置时无法判断该拉黑谁。
SUBJECT_USER = "user"

# ---- 业务结论（单选，docs/PRD.md §9.3）----
BIZ_APPROVE = "approve"
BIZ_REJECT = "reject"
ALL_BIZ_RESULTS: tuple[str, ...] = (BIZ_APPROVE, BIZ_REJECT)

# ---- 风控动作（多选，docs/PRD.md §9.3）----
RISK_PASS = "pass"
RISK_BLOCK_ORDER = "block_order"
RISK_BLACKLIST_USER = "blacklist_user"
RISK_BAN_DEVICE = "ban_device"
RISK_WATCHLIST_ADD = "watchlist_add"

ALL_RISK_ACTIONS: tuple[str, ...] = (
    RISK_PASS,
    RISK_BLOCK_ORDER,
    RISK_BLACKLIST_USER,
    RISK_BAN_DEVICE,
    RISK_WATCHLIST_ADD,
)

# ``pass`` 是"不追加任何措施"，与其它动作在语义上互斥：
# 同时勾选 pass 与 block_order 时，执行顺序会让结果完全不同（先放行再拦截？），
# 因此提交校验直接拒绝这种组合，而不是替审核员猜一个执行顺序。
# 副作用动作之间则允许并存（拉黑账号 + 封设备 + 拦订单是常见组合）。
EXCLUSIVE_RISK_ACTION = RISK_PASS

# ---- 处置动作执行结果 ----
EXEC_SUCCESS = "success"
EXEC_FAILED = "failed"
EXEC_SKIPPED = "skipped"

# 处置备注最短字数（docs/PRD.md §9.3：≥10 字）。
# 门槛设在服务层而非仅靠前端：界面校验能被直接调接口绕过，
# 而"处置原因"是事后追责的唯一人证。
MIN_REMARK_LENGTH = 10


class RcCase(Base):
    """案件（合案主体，工作台左栏列表的数据源）。"""

    __tablename__ = "rc_case"
    __table_args__ = (
        # 合案候选查询：同主体 + 同场景 + 未结 + 时间窗内。
        # 列顺序与查询条件严格对齐（subject_type/value/scene 等值 + status 等值 + last_at 范围），
        # 否则这个索引只能吃到最左前缀，回表量会随案件沉淀量线性上升。
        Index("ix_rc_case_subject_status", "subject_type", "subject_value", "scene", "status", "last_at"),
        # 列表页默认排序与"按状态筛选 + 时间倒序"走这条
        Index("ix_rc_case_status_time", "status", "last_at"),
        Index("ix_rc_case_scene_status", "scene", "status", "last_at"),
        # "我的案件"筛选
        Index("ix_rc_case_handler_status", "handler_id", "status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="案件编号，对外主键")
    subject_type: Mapped[str] = mapped_column(String(16), default=SUBJECT_USER, nullable=False)
    subject_value: Mapped[str] = mapped_column(String(64), nullable=False, comment="主体值（P1 为 user_id）")
    scene: Mapped[str] = mapped_column(String(32), nullable=False, comment="规则场景：login/coupon/order/after_sale")
    status: Mapped[str] = mapped_column(String(16), default=CASE_PENDING, nullable=False)

    risk_level: Mapped[str] = mapped_column(String(8), default="low", nullable=False, comment="案件内最高风险等级")
    max_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="案件内最高综合分")
    hit_cnt: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="累计命中规则条数")
    event_cnt: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="关联事件数")

    first_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False, comment="首次触发时间")
    last_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False, comment="末次触发时间，合案窗口以它为准")
    last_event_id: Mapped[str | None] = mapped_column(String(48), comment="最近一次关联事件，详情页默认展开它")
    last_decision_id: Mapped[str | None] = mapped_column(String(48))

    # handler / handler_id 同时存：列表页直接展示人名（不做 join），
    # 权限判断用 id（人名可能重复或改名）。
    handler: Mapped[str | None] = mapped_column(String(64), comment="当前处理人用户名，未接手为空")
    handler_id: Mapped[int | None] = mapped_column(BigInteger)
    claimed_at: Mapped[object | None] = mapped_column(DateTime)
    disposed_at: Mapped[object | None] = mapped_column(DateTime)
    archived_at: Mapped[object | None] = mapped_column(DateTime)
    closed_at: Mapped[object | None] = mapped_column(DateTime)
    close_reason: Mapped[str | None] = mapped_column(String(255))

    dispose_result: Mapped[str | None] = mapped_column(String(16), comment="冗余的最终业务结论：approve/reject")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class RcCaseEvent(Base):
    """案件与事件的关联（工作台的证据时间线）。"""

    __tablename__ = "rc_case_event"
    __table_args__ = (
        # 合案重试/并发的幂等兜底：同一案件绝不会挂两次同一事件。
        # 没有这条唯一约束时，一次 flush 失败重试就会让时间线出现重复条目，
        # 而重复条目看起来像"用户触发了两次"，会误导研判。
        Index("uq_rc_case_event_case_event", "case_no", "event_id", unique=True),
        Index("ix_rc_case_event_case_time", "case_no", "occurred_at"),
        Index("ix_rc_case_event_event", "event_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(32), nullable=False)
    event_id: Mapped[str] = mapped_column(String(48), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(48), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scene: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="该事件的规则命中条数")
    biz_no: Mapped[str | None] = mapped_column(String(32), comment="关联业务单据号，处置联动按它定位订单/退款单")
    occurred_at: Mapped[object] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class RcCaseAction(Base):
    """一次处置提交（双维度结论 + 备注 + 操作者）。"""

    __tablename__ = "rc_case_action"
    __table_args__ = (
        Index("ix_rc_case_action_case_time", "case_no", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_no: Mapped[str] = mapped_column(String(32), nullable=False)
    business_result: Mapped[str] = mapped_column(String(16), nullable=False, comment="approve/reject")
    # risk_actions 冗余存一份动作清单：明细表里每项动作的"执行结果"才是权威，
    # 但处置详情页要按提交时的顺序原样回显勾选项，逐项 order by 反而会打乱顺序。
    risk_actions: Mapped[list] = mapped_column(JSON, nullable=False)
    remark: Mapped[str] = mapped_column(String(500), nullable=False, comment="处置原因备注（≥10 字）")
    operator_id: Mapped[int | None] = mapped_column(BigInteger)
    operator_name: Mapped[str | None] = mapped_column(String(64))
    actor_role: Mapped[str | None] = mapped_column(String(32))
    status_before: Mapped[str] = mapped_column(String(16), default=CASE_PROCESSING, nullable=False)
    status_after: Mapped[str] = mapped_column(String(16), default=CASE_DISPOSED, nullable=False)
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class RcCaseActionItem(Base):
    """一项风控动作的执行结果（含联动失败原因）。"""

    __tablename__ = "rc_case_action_item"
    __table_args__ = (
        Index("ix_rc_case_action_item_action", "action_id"),
        Index("ix_rc_case_action_item_case", "case_no"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    action_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    case_no: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_action: Mapped[str] = mapped_column(String(32), nullable=False)
    exec_result: Mapped[str] = mapped_column(String(16), nullable=False, comment="success/failed/skipped")
    # target 记录"这次动作改了哪一行"：处置没生效时，复盘的第一问就是
    # "它当时到底指向谁"，只留一个失败原因是回答不了的。
    target_type: Mapped[str | None] = mapped_column(String(32), comment="order/refund/customer/list_entry")
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON, comment="联动明细/失败原因，前端直接展示")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
