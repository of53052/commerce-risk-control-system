"""融合与仲裁测试：三种融合模式、阈值边界、Challenge 规则、名单直判。

重点覆盖的风险（这些都是"差一分就换一个人工审核队列"的边界）：
    1. 阈值边界必须闭合（>= 而非 >），否则 60 分的事件会被判为低风险放行；
    2. Challenge 只能由规则显式声明，不能被分数区间隐式触发；
    3. 名单直判时规则分与模型分必须归零，避免审核员误判"规则也参与了"；
    4. 未知 fusion_mode 要留痕，不能静默按默认模式走。
"""

from __future__ import annotations

import pytest

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
from app.services.fusion import (
    FUSION_ADDITIVE,
    FUSION_MAX,
    FUSION_WEIGHTED,
    FusionInput,
    combine_scores,
    decide,
    needs_case,
)


@pytest.mark.parametrize(
    ("risk_score", "action", "level"),
    [
        (0, ACTION_PASS, RISK_LOW),
        (59, ACTION_PASS, RISK_LOW),
        (60, ACTION_REVIEW, RISK_MID),      # 阈值闭合：等于即进入中风险
        (79, ACTION_REVIEW, RISK_MID),
        (80, ACTION_REJECT, RISK_HIGH),     # 阈值闭合
        (100, ACTION_REJECT, RISK_HIGH),
    ],
)
def test_threshold_boundaries(risk_score: int, action: str, level: str) -> None:
    """用 alpha=1 构造"规则分 = 综合分"的场景，直接测阈值。"""
    result = decide(FusionInput(rule_score=risk_score, model_score=0, alpha=1.0))
    assert result.risk_score == risk_score
    assert result.action == action
    assert result.risk_level == level


def test_challenge_only_from_rule_hint() -> None:
    """落在中风险区间但规则没声明 challenge -> Review。"""
    result = decide(FusionInput(rule_score=70, model_score=0, alpha=1.0, challenge_hint=False))
    assert result.action == ACTION_REVIEW
    assert result.action_hint == "none"


def test_challenge_when_rule_declares() -> None:
    result = decide(FusionInput(rule_score=70, model_score=0, alpha=1.0, challenge_hint=True))
    assert result.action == ACTION_CHALLENGE
    assert result.action_hint == "challenge"


def test_challenge_hint_ignored_outside_mid_range() -> None:
    """高分区间即使声明了 challenge 也必须是 Reject —— 分数优先。"""
    result = decide(FusionInput(rule_score=95, model_score=0, alpha=1.0, challenge_hint=True))
    assert result.action == ACTION_REJECT


@pytest.mark.parametrize(
    ("mode", "rule_score", "model_score", "alpha", "expected"),
    [
        (FUSION_ADDITIVE, 50, 100, 0.3, 80),      # 50 + 30
        (FUSION_ADDITIVE, 70, 100, 0.3, 100),     # 70 + 30 = 100
        (FUSION_ADDITIVE, 90, 100, 0.3, 100),     # 120 -> clamp 100
        (FUSION_MAX, 50, 100, 0.3, 50),           # max(50, 30)
        (FUSION_MAX, 20, 100, 0.3, 30),           # max(20, 30)
        (FUSION_WEIGHTED, 50, 100, 0.3, 65),      # 0.7*50 + 0.3*100
        (FUSION_WEIGHTED, 100, 0, 0.3, 70),       # 规则被压低，阈值需重标定
    ],
)
def test_fusion_modes(mode: str, rule_score: int, model_score: int, alpha: float, expected: int) -> None:
    score, _notes = combine_scores(rule_score, model_score, alpha=alpha, mode=mode)
    assert score == expected


def test_unknown_mode_falls_back_with_note() -> None:
    score, notes = combine_scores(50, 100, alpha=0.3, mode="add")
    assert score == 80
    assert notes and "未知融合模式" in notes[0]


def test_weighted_mode_emits_calibration_note() -> None:
    _score, notes = combine_scores(50, 100, alpha=0.3, mode=FUSION_WEIGHTED)
    assert any("阈值需配套标定" in note for note in notes)


def test_alpha_clamped_to_unit_interval() -> None:
    high, _ = combine_scores(50, 100, alpha=5.0, mode=FUSION_ADDITIVE)
    assert high == 100
    low, _ = combine_scores(50, 100, alpha=-1.0, mode=FUSION_ADDITIVE)
    assert low == 50


def test_scores_clamped_to_range() -> None:
    result = decide(FusionInput(rule_score=999, model_score=-50, alpha=1.0))
    assert result.rule_score == 100
    assert result.model_score == 0


@pytest.mark.parametrize(
    ("list_decision", "action", "level", "score"),
    [
        (ACTION_REJECT, ACTION_REJECT, RISK_HIGH, 100),
        (ACTION_PASS, ACTION_PASS, RISK_LOW, 0),
    ],
)
def test_list_direct_decision_zeroes_scores(list_decision: str, action: str, level: str, score: int) -> None:
    """名单直判：跳过规则与模型，两个分归零并标注 decided_by=list。"""
    result = decide(
        FusionInput(
            rule_score=80,
            model_score=90,
            list_decision=list_decision,
            alpha=0.3,
        )
    )
    assert result.action == action
    assert result.risk_level == level
    assert result.rule_score == 0
    assert result.model_score == 0
    assert result.risk_score == score
    assert result.decided_by == DECIDED_BY_LIST


def test_list_conflict_adds_note() -> None:
    result = decide(
        FusionInput(list_decision=ACTION_REJECT, list_conflict=True, alpha=0.3)
    )
    assert any("冲突策略" in note for note in result.notes)


def test_model_disabled_note() -> None:
    result = decide(FusionInput(rule_score=10, model_score=0, model_disabled=True, alpha=0.3))
    assert result.decided_by == DECIDED_BY_RULE_MODEL
    assert any("无启用模型" in note for note in result.notes)


def test_model_version_recorded() -> None:
    result = decide(FusionInput(rule_score=10, model_score=20, model_version="v3", alpha=0.3))
    assert result.model_version == "v3"


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (ACTION_REVIEW, True),
        (ACTION_REJECT, True),
        (ACTION_CHALLENGE, False),   # 二次验证不建案
        (ACTION_PASS, False),
    ],
)
def test_needs_case(action: str, expected: bool) -> None:
    result = decide(FusionInput(rule_score=0, model_score=0, alpha=0.3))
    result.action = action
    assert needs_case(result) is expected
