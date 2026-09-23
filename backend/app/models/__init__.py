"""ORM 模型包。

作用：把全部模型汇总导入，供 Alembic autogenerate 采集元数据。
新增模型时务必在此登记，否则迁移会漏表（这是 autogenerate 最常见的坑）。
"""

from app.models.audit import RcAuditLog
from app.models.biz import (
    BizCouponReceive,
    BizCustomer,
    BizOrder,
    BizProduct,
    BizRefund,
)
from app.models.decision import (
    RcDecision,
    RcDecisionHit,
    RcModelContribution,
    RcModelVersion,
)
from app.models.event import RcEvent, RcFeatureSnapshot
from app.models.rclist import RcListEntry
from app.models.rule import RcRule, RcRuleVersion
from app.models.sys import SysApiKey, SysConfig, SysUser

__all__ = [
    # 系统域
    "SysUser",
    "SysConfig",
    "SysApiKey",
    # 业务域（模拟电商业务系统）
    "BizCustomer",
    "BizProduct",
    "BizOrder",
    "BizRefund",
    "BizCouponReceive",
    # 风控事实域
    "RcEvent",
    "RcFeatureSnapshot",
    "RcDecision",
    "RcDecisionHit",
    "RcModelContribution",
    "RcModelVersion",
    # 风控策略域
    "RcRule",
    "RcRuleVersion",
    "RcListEntry",
    # 审计
    "RcAuditLog",
]
