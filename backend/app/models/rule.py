"""策略域模型：规则与规则版本。

设计要点：
- condition 存 AST JSON（唯一真源），condition_text 只存"文本形态"用于界面回显；
  两者由同一个解析器保证等价，避免出现"文本改了但 AST 没改"的双写不一致。
- 每次修改规则写一条 rc_rule_version 快照：策略回滚与"谁在什么时候改了什么"都靠它。
- action_hint 是 Challenge 的唯一来源（docs/PRD.md §8.4 明确：不做隐式分数区间映射）。
"""

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

ACTION_HINT_NONE = "none"
ACTION_HINT_CHALLENGE = "challenge"

CATEGORY_LIST = "list"
CATEGORY_FREQUENCY = "frequency"
CATEGORY_ENVIRONMENT = "environment"
CATEGORY_AFTERSALE = "aftersale"
CATEGORY_AMOUNT = "amount"

ALL_CATEGORIES = (CATEGORY_LIST, CATEGORY_FREQUENCY, CATEGORY_ENVIRONMENT, CATEGORY_AFTERSALE, CATEGORY_AMOUNT)


class RcRule(Base):
    """风控规则。"""

    __tablename__ = "rc_rule"
    __table_args__ = (
        Index("ix_rc_rule_scene_enabled", "scene", "enabled"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, comment="规则编码，如 RC_ENV_001")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, comment="适用场景：coupon/order/after_sale/login/all")
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    condition: Mapped[dict] = mapped_column(JSON, nullable=False, comment="AST，唯一真源")
    condition_text: Mapped[str] = mapped_column(Text, nullable=False, comment="表达式的文本形态，便于阅读与编辑")
    score: Mapped[int] = mapped_column(Integer, nullable=False, comment="命中后累加的分值")
    action_hint: Mapped[str] = mapped_column(String(16), default=ACTION_HINT_NONE, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="数值越小越先求值")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(64))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[object] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class RcRuleVersion(Base):
    """规则版本快照（只增不删）。"""

    __tablename__ = "rc_rule_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rule_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    condition: Mapped[dict] = mapped_column(JSON, nullable=False)
    condition_text: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    action_hint: Mapped[str] = mapped_column(String(16), default=ACTION_HINT_NONE, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    change_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="create/update/toggle/rollback")
    changed_by: Mapped[str | None] = mapped_column(String(64))
    changed_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
