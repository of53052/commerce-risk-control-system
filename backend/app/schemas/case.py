"""案件接口的请求模型（docs/PRD.md §9、§12）。

只放**请求结构**：响应直接在 ``app/api/case.py`` 里由服务层字典组装，
因为详情结构（画像/单据/特征/图谱/证据）是给前端消费的组合对象，
再给它套一层严格 Pydantic 模型会把"改动一个查询字段"变成"改三处"。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.case import (
    ALL_BIZ_RESULTS,
    ALL_RISK_ACTIONS,
    EXCLUSIVE_RISK_ACTION,
    MIN_REMARK_LENGTH,
)

BizResult = Literal["approve", "reject"]
RiskAction = Literal[
    "pass", "block_order", "blacklist_user", "ban_device", "watchlist_add"
]


class DisposeIn(BaseModel):
    """提交一次案件处置。

    ``risk_actions`` 至少一项；``pass``（不追加措施）与其他动作互斥，
    交互抽屉里须保证勾选项不会以这种组合到达后端（这里是服务端兜底）。
    """

    model_config = ConfigDict(extra="forbid")

    business_result: BizResult
    risk_actions: list[RiskAction] = Field(min_length=1)
    remark: str = Field(min_length=MIN_REMARK_LENGTH, max_length=500)

    @field_validator("risk_actions")
    @classmethod
    def _pass_must_stand_alone(cls, values):  # type: ignore[no-untyped-def]
        if EXCLUSIVE_RISK_ACTION in values and len(values) > 1:
            raise ValueError(f"`{EXCLUSIVE_RISK_ACTION}` 不能与其他动作同时选择")
        return values


class ArchiveIn(BaseModel):
    """批量归档请求。"""

    model_config = ConfigDict(extra="forbid")

    case_nos: list[str] = Field(min_length=1, max_length=100)


class CloseIn(BaseModel):
    """管理员强制关闭请求（docs/PRD.md §9.2：原因必填）。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=255)
