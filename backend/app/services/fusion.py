"""融合与仲裁：把规则分、模型分、名单结论合成最终动作。

公式（docs/PRD.md §8.4 / docs/ARCHITECTURE.md §7.4）::

    rule_score  = min(sum(hit.score), 100)
    model_score = round(100 * sigmoid(w·z + b))            # 无启用模型时为 0
    risk_score  = clamp(round(融合(rule_score, model_score)), 0, 100)

    action:
        decided_by == list          -> 名单结论（Reject / Pass）
        risk_score >= reject_thr    -> Reject
        risk_score >= review_thr    -> Challenge（规则显式声明）否则 Review
        否则                        -> Pass

三种融合模式：

| 模式 | 公式 | 语义 |
| --- | --- | --- |
| ``additive``（默认） | ``rule + α·model`` | 规则为主，模型作为"最多加 α·100 分"的加分项 |
| ``max`` | ``max(rule, α·model)`` | 只取最强单一信号，模型仍按 α 折算（保留权重的含义） |
| ``weighted`` | ``(1-α)·rule + α·model`` | 共识模式：两个引擎各占权重，任一引擎单独报警不足以定级 |

**``weighted`` 的代价必须说清楚**：它会把规则分整体压低（α=0.3 时 100 分的规则
只剩 70），因此 60/80 这两个阈值在切到 weighted 后必须重新标定 ——
模式与阈值是配套参数，不能只改一个。这一条写在这里而不是靠口口相传。

**Challenge 只由规则显式声明**：不做"某分数区间即 Challenge"的隐式映射，
否则策略师改一条规则的分数就可能把流量在"人工审核"与"二次验证"之间悄悄挪动，
而配置界面上完全看不出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.decision import (
    ACTION_CHALLENGE,
    ACTION_PASS,
    ACTION_REJECT,
    ACTION_REVIEW,
    DECIDED_BY_LIST,
    DECIDED_BY_RULE_MODEL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MID,
)

FUSION_ADDITIVE = "additive"
FUSION_MAX = "max"
FUSION_WEIGHTED = "weighted"


@dataclass
class FusionInput:
    """融合输入（避免函数参数膨胀，同时让调用点更可读）。"""

    rule_score: int = 0
    model_score: int = 0
    list_decision: str | None = None
    list_conflict: bool = False
    challenge_hint: bool = False
    review_threshold: int = 60
    reject_threshold: int = 80
    alpha: float = 0.3
    mode: str = FUSION_ADDITIVE
    model_version: str | None = None
    model_disabled: bool = False


@dataclass
class FusionResult:
    """融合结论（直接对应 rc_decision 的字段）。"""

    rule_score: int
    model_score: int
    risk_score: int
    risk_level: str
    action: str
    action_hint: str
    decided_by: str
    fusion_alpha: float
    fusion_mode: str
    model_version: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_score": self.rule_score,
            "model_score": self.model_score,
            "risk_score": self.risk_score,
            "risk_level": self.risk_level,
            "action": self.action,
            "action_hint": self.action_hint,
            "decided_by": self.decided_by,
            "fusion_alpha": self.fusion_alpha,
            "fusion_mode": self.fusion_mode,
            "model_version": self.model_version,
            "notes": self.notes,
        }


def combine_scores(rule_score: int, model_score: int, *, alpha: float, mode: str) -> tuple[int, list[str]]:
    """按模式融合两个分，返回 (综合分, 备注)。"""
    notes: list[str] = []
    alpha = max(0.0, min(float(alpha), 1.0))

    if mode == FUSION_MAX:
        raw = max(rule_score, alpha * model_score)
    elif mode == FUSION_WEIGHTED:
        raw = (1 - alpha) * rule_score + alpha * model_score
        notes.append("weighted 模式会整体压低规则分，阈值需配套标定")
    else:
        if mode != FUSION_ADDITIVE:
            # 未知模式不静默当成默认值：策略师把 fusion_mode 写成 "add" 之类的
            # 拼写错误时，如果悄悄按 additive 走，风控行为与配置不符且无人知晓。
            notes.append(f"未知融合模式 {mode!r}，已回退 additive")
        raw = rule_score + alpha * model_score

    return int(round(max(0.0, min(raw, 100.0)))), notes


def decide(inputs: FusionInput) -> FusionResult:
    """融合 + 仲裁，产出最终动作。"""
    rule_score = max(0, min(int(inputs.rule_score), 100))
    model_score = max(0, min(int(inputs.model_score), 100))

    notes: list[str] = []
    if inputs.model_disabled:
        notes.append("无启用模型，model_score 记 0")

    risk_score, combine_notes = combine_scores(
        rule_score, model_score, alpha=inputs.alpha, mode=inputs.mode
    )
    notes.extend(combine_notes)

    # ---- 名单直判：跳过规则与模型 ----
    if inputs.list_decision is not None:
        action = inputs.list_decision
        # 名单直判时规则分与模型分置 0：它们是"这次没参与决策"的证据，
        # 留着上一次的值会让审核员误以为规则也参与了。
        rule_score = 0
        model_score = 0
        risk_score = 100 if action == ACTION_REJECT else 0
        risk_level = RISK_HIGH if action == ACTION_REJECT else RISK_LOW
        if inputs.list_conflict:
            notes.append("黑白名单同时命中，已按冲突策略仲裁")
        return FusionResult(
            rule_score=rule_score,
            model_score=model_score,
            risk_score=risk_score,
            risk_level=risk_level,
            action=action,
            action_hint="none",
            decided_by=DECIDED_BY_LIST,
            fusion_alpha=float(inputs.alpha),
            fusion_mode=inputs.mode,
            model_version=None,
            notes=notes,
        )

    # ---- 分数区间仲裁 ----
    action_hint = "none"
    if risk_score >= inputs.reject_threshold:
        action = ACTION_REJECT
        risk_level = RISK_HIGH
    elif risk_score >= inputs.review_threshold:
        risk_level = RISK_MID
        if inputs.challenge_hint:
            # Challenge 不建案（docs/PRD.md §8.4：仅由规则显式声明触发）
            action = ACTION_CHALLENGE
            action_hint = "challenge"
        else:
            action = ACTION_REVIEW
    else:
        action = ACTION_PASS
        risk_level = RISK_LOW

    return FusionResult(
        rule_score=rule_score,
        model_score=model_score,
        risk_score=risk_score,
        risk_level=risk_level,
        action=action,
        action_hint=action_hint,
        decided_by=DECIDED_BY_RULE_MODEL,
        fusion_alpha=float(inputs.alpha),
        fusion_mode=inputs.mode,
        model_version=inputs.model_version,
        notes=notes,
    )


def needs_case(result: FusionResult) -> bool:
    """是否需要生成/合并案件。

    Review 与 Reject 建案；Challenge 与 Pass 不建案。
    Challenge 不建案是刻意的：二次验证的结果会以新事件的形式回来，
    "还没验证就先建案"会让人工队列被验证中的流量淹没。
    """
    return result.action in {ACTION_REVIEW, ACTION_REJECT}

