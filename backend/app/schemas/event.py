"""事件接入的请求/响应模型（docs/PRD.md §6.1、§6.3）。

校验分两层：
    1. **结构校验**（本模块）：字段存在性、类型、枚举取值；
    2. **业务校验**（event_gateway）：event_id 幂等、时间偏差告警、payload 必填项。

分开的理由：结构错误应该被 FastAPI 直接挡在 422，而业务校验（比如"时间偏差"）
需要在业务上下文里处理并留痕，两者混在一起会让"哪些错误该返回 422"变得模糊。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.event import ALL_EVENT_TYPES

EventType = Literal["login", "coupon_receive", "order_create", "order_pay", "after_sale_apply"]


class DeviceInfo(BaseModel):
    """设备信息。``fingerprint`` 用宽松 dict：指纹字段会随采集端演进，
    用强类型会把"新增一个指纹维度"变成一次接口变更。
    """

    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1, max_length=64)
    fingerprint: dict[str, Any] = Field(default_factory=dict)


class NetworkInfo(BaseModel):
    """网络信息。"""

    model_config = ConfigDict(extra="forbid")

    ip: str = Field(min_length=1, max_length=64)
    ip_region: str | None = Field(default=None, max_length=64)
    is_proxy: bool = False


class AddressInfo(BaseModel):
    """收货信息（可选：登录、领券事件通常没有）。"""

    model_config = ConfigDict(extra="forbid")

    receiver_name: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    text: str | None = None
    address_hash: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)


class EventIn(BaseModel):
    """单条事件（通用信封）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=8, max_length=48, description="全局唯一，幂等键")
    event_type: EventType
    occurred_at: datetime = Field(description="业务发生时间（ISO8601，带时区则按 UTC 换算）")
    user_id: str = Field(min_length=1, max_length=32)
    phone: str = Field(min_length=1, max_length=20)
    device: DeviceInfo
    network: NetworkInfo
    address: AddressInfo | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source: Literal["real", "simulation"] = "real"

    @field_validator("event_type")
    @classmethod
    def _check_event_type(cls, value: str) -> str:
        # Literal 已经限制了取值，这里再对一次是为了让错误信息里带上完整枚举，
        # 前端可以直接提示"支持哪些事件类型"
        if value not in ALL_EVENT_TYPES:
            raise ValueError(f"不支持的事件类型：{value}，可选 {list(ALL_EVENT_TYPES)}")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _to_naive_utc(cls, value: datetime) -> datetime:
        """统一成 naive UTC。
        
        带时区的输入先换算到 UTC 再去掉 tzinfo：库里存的是 naive UTC
        （见 app/core/timeutil.py 的约定），如果这里放行带时区的 datetime，
        同一条事件在不同代码路径下会得到不同的窗口边界。
        """
        if value.tzinfo is not None:
            from datetime import timezone

            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.replace(microsecond=0)


class EventBatchIn(BaseModel):
    """批量事件（上限 200 条：单批过大时请求体会撑爆内存与事务时长）。"""

    model_config = ConfigDict(extra="forbid")

    events: list[EventIn] = Field(min_length=1, max_length=200)


class RuleHitOut(BaseModel):
    """规则命中明细（docs/PRD.md §8.5）。"""

    rule_code: str
    rule_name: str
    rule_category: str | None = None
    score: int
    reason: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    # 缺失字段与求值错误随命中一起返回：审核员看到"命中"时，
    # 也需要知道这条规则是不是在"有字段没数据"的情况下命中的（证据强度不同）。
    missing_fields: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ModelContributionOut(BaseModel):
    feature_name: str
    feature_value: float | None = None
    contribution: float
    direction: str
    rank_no: int


class DecisionOut(BaseModel):
    """决策结果（docs/PRD.md §8.5 的对外结构）。"""

    decision_id: str
    event_id: str
    event_type: str
    user_id: str
    biz_no: str | None = None
    occurred_at: datetime

    rule_score: int
    model_score: int
    risk_score: int
    risk_level: str
    action: str
    action_hint: str = "none"
    decided_by: str
    case_no: str | None = None
    model_version: str | None = None

    rule_hits: list[RuleHitOut] = Field(default_factory=list)
    # features 参与打分；context 只提供证据上下文（含脱敏手机号与原始 payload）。
    # 两者刻意不合并，避免把"展示用事实"误读成判据。
    features: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    model_contributions: list[ModelContributionOut] = Field(default_factory=list)
    list_hits: list[dict[str, Any]] = Field(default_factory=list)

    latency_ms: int | None = None
    from_cache: bool = False
    source: str = "real"
    cost_ms: int | None = Field(default=None, description="特征计算耗时（ms）")
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EventAcceptedOut(BaseModel):
    """事件接入响应。"""

    accepted: bool = True
    duplicated: bool = False
    decision: DecisionOut


class BatchAcceptedOut(BaseModel):
    """批量接入响应。"""

    total: int
    accepted: int
    duplicated: int
    decisions: list[DecisionOut] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class ErrorOut(BaseModel):
    """统一错误结构（docs/PRD.md §6.3：错误码 + 字段路径 + 原因）。"""

    code: str
    message: str
    field: str | None = None
    detail: Any = None
