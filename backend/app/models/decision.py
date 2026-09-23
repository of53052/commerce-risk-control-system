"""决策域模型：决策、命中明细、模型贡献、模型版本。

设计要点：
- 三个分（rule_score / model_score / risk_score）全部落库：这是"可解释"的物理基础。
  只存综合分的话，审核员问"凭什么 87 分"时无法回答。
- decided_by 记录结论来源（list / rule_model），名单直判时规则分与模型分为 0。
- rc_model_contribution 只存 top-5：全量贡献对排障无增益，却会让表体积膨胀一个数量级。
- rank_no 而非 rank：RANK 是 MySQL 8 保留字，直接用会踩语法坑（docs/PRD.md 附录 A）。
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.timeutil import utcnow
from app.db.base import Base

RISK_LOW = "low"
RISK_MID = "mid"
RISK_HIGH = "high"

ACTION_PASS = "Pass"
ACTION_CHALLENGE = "Challenge"
ACTION_REVIEW = "Review"
ACTION_REJECT = "Reject"

DECIDED_BY_LIST = "list"
DECIDED_BY_RULE_MODEL = "rule_model"


class RcDecision(Base):
    """决策结果（一次事件一条，与 rc_event 1:1）。"""

    __tablename__ = "rc_decision"
    __table_args__ = (
        Index("ix_rc_decision_created", "created_at"),
        Index("ix_rc_decision_action_time", "action", "created_at"),
        Index("ix_rc_decision_level_time", "risk_level", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False, comment="对外暴露的决策编号")
    event_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    rule_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    risk_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(8), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    action_hint: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    decided_by: Mapped[str] = mapped_column(String(16), default=DECIDED_BY_RULE_MODEL, nullable=False)
    fusion_alpha: Mapped[float] = mapped_column(Numeric(4, 3), default=0.3, nullable=False, comment="本次使用的融合权重，便于复算")
    fusion_mode: Mapped[str] = mapped_column(String(16), default="additive", nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(16))
    case_no: Mapped[str | None] = mapped_column(String(32), index=True, comment="若生成/合并了案件")
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="命中规则条数")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否幂等缓存命中")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class RcDecisionHit(Base):
    """规则命中明细。"""

    __tablename__ = "rc_decision_hit"
    __table_args__ = (
        Index("ix_rc_decision_hit_rule_time", "rule_code", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_general_ci"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(48), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_category: Mapped[str | None] = mapped_column(String(32))
    score: Mapped[int] = mapped_column(Integer, nullable=False, comment="该规则本次贡献的分值")
    # 注意：comment 内使用了中文书名号而非 ASCII 双引号 —— 在双引号字符串里再塞 ASCII 双引号会直接语法错误。
    reason: Mapped[str | None] = mapped_column(String(255), comment="可读的触发原因，如「同设备 24h 关联 7 个账号」")
    evidence: Mapped[dict | None] = mapped_column(JSON, comment="逐条件求值结果，供证据链展示")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class RcModelContribution(Base):
    """模型贡献度 top-5（正贡献推高风险，负贡献拉低）。"""

    __tablename__ = "rc_model_contribution"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    feature_name: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_value: Mapped[float | None] = mapped_column(Numeric(18, 6))
    contribution: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, comment="w_i * z_i")
    direction: Mapped[str] = mapped_column(String(8), default="up", nullable=False, comment="up/down")
    rank_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="1 = 影响最大")
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)


class RcModelVersion(Base):
    """模型版本登记（模型本体是 backend/models_artifacts/model.json 文件）。"""

    __tablename__ = "rc_model_version"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    feature_names: Mapped[list] = mapped_column(JSON, nullable=False, comment="特征顺序，推理时严格按此顺序取数")
    metrics: Mapped[dict | None] = mapped_column(JSON, comment="auc/ks/precision_at_100/samples")
    sample_count: Mapped[int | None] = mapped_column(Integer)
    trained_at: Mapped[object | None] = mapped_column(DateTime)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="同一时刻仅允许一个启用版本")
    remark: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[object] = mapped_column(DateTime, default=utcnow, nullable=False)
