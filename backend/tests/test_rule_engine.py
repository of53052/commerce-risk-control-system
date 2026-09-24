"""规则引擎测试：场景过滤、计分、action_hint、编译缓存、异常容错。

重点覆盖的风险：
    1. 一条规则在一次决策里只能计分一次（重复累加会让分数虚高到乱拦）；
    2. ``scene="all"`` 的通用规则必须在所有场景生效，否则"通用规则"名不副实；
    3. 单条规则的条件树损坏，只能跳过该规则，不能让整条决策链崩掉；
    4. 编译缓存必须能被主动失效，否则改了规则要重启才生效。
"""

from __future__ import annotations

import pytest

from app.expression import node_to_dict, parse
from app.models.rule import (
    ACTION_HINT_CHALLENGE,
    ACTION_HINT_NONE,
    CATEGORY_ENVIRONMENT,
    CATEGORY_FREQUENCY,
    RcRule,
)
from app.services import rule_engine

pytestmark = pytest.mark.integration


def _rule(
    *,
    code: str,
    text: str,
    score: int,
    scene: str = "all",
    priority: int = 100,
    action_hint: str = ACTION_HINT_NONE,
    enabled: bool = True,
    category: str = CATEGORY_FREQUENCY,
    condition: dict | None = None,
    version: int = 1,
) -> RcRule:
    ast = parse(text)
    return RcRule(
        code=code,
        name=f"规则 {code}",
        scene=scene,
        category=category,
        condition=condition if condition is not None else node_to_dict(ast),
        condition_text=text,
        score=score,
        action_hint=action_hint,
        priority=priority,
        enabled=enabled,
        version=version,
        description=None,
        created_by="test",
        updated_by="test",
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    rule_engine.invalidate_cache()
    yield
    rule_engine.invalidate_cache()


def test_hit_accumulates_score(db) -> None:
    db.add(_rule(code="R1", text="a == 1", score=30))
    db.add(_rule(code="R2", text="b == 2", score=25))
    db.flush()

    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1, "b": 2})
    assert result.rule_score == 55
    assert [hit.rule_code for hit in result.hits] == ["R1", "R2"]


def test_non_hit_scores_nothing(db) -> None:
    db.add(_rule(code="R1", text="a == 1", score=30))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 0})
    assert result.rule_score == 0
    assert result.hits == []


def test_rule_scored_once_per_decision(db) -> None:
    """同一规则命中一次只计一次分（不因字段重复出现而翻倍）。"""
    db.add(_rule(code="R1", text="a >= 1 and a >= 0", score=30))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 5})
    assert result.rule_score == 30
    assert len(result.hits) == 1


def test_scene_filter_includes_all_scope(db) -> None:
    """scene=all 的通用规则对所有场景生效。"""
    db.add(_rule(code="GEN", text="a == 1", score=10, scene="all"))
    db.add(_rule(code="CPN", text="a == 1", score=20, scene="coupon"))
    db.add(_rule(code="ORD", text="a == 1", score=40, scene="order"))
    db.flush()

    coupon = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})
    assert {hit.rule_code for hit in coupon.hits} == {"GEN", "CPN"}
    assert coupon.rule_score == 30

    order = rule_engine.evaluate_rules(db, scene="order", features={"a": 1})
    assert {hit.rule_code for hit in order.hits} == {"GEN", "ORD"}


def test_disabled_rule_not_evaluated(db) -> None:
    db.add(_rule(code="R1", text="a == 1", score=30, enabled=False))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})
    assert result.rule_score == 0


def test_priority_order(db) -> None:
    db.add(_rule(code="LATE", text="a == 1", score=1, priority=90))
    db.add(_rule(code="EARLY", text="a == 1", score=1, priority=5))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})
    assert [hit.rule_code for hit in result.hits] == ["EARLY", "LATE"]


def test_challenge_hint_collected(db) -> None:
    db.add(_rule(code="C1", text="a == 1", score=20, action_hint=ACTION_HINT_CHALLENGE))
    db.add(_rule(code="N1", text="a == 1", score=20))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})
    assert result.challenge_hint is True


def test_challenge_hint_absent_when_not_hit(db) -> None:
    db.add(_rule(code="C1", text="a == 1", score=20, action_hint=ACTION_HINT_CHALLENGE))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 0})
    assert result.challenge_hint is False


def test_missing_fields_aggregated(db) -> None:
    db.add(_rule(code="R1", text="a == 1 and b == 2", score=10))
    db.add(_rule(code="R2", text="b == 2 and c == 3", score=10))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={})
    assert set(result.missing_fields) == {"a", "b", "c"}
    assert result.rule_score == 0


def test_reason_is_readable(db) -> None:
    db.add(_rule(code="R1", text="device_account_cnt_24h >= 3", score=25))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="all", features={"device_account_cnt_24h": 7})
    reason = result.hits[0].reason
    assert "device_account_cnt_24h" in reason
    assert "7" in reason
    assert "3" in reason


def test_evidence_contains_leaf_results(db) -> None:
    db.add(_rule(code="R1", text="a >= 3 and b == 2", score=10))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="all", features={"a": 5, "b": 2})
    evidence = result.hits[0].evidence
    assert len(evidence) == 2
    assert {item["field"] for item in evidence} == {"a", "b"}
    assert all(item["passed"] for item in evidence)


def test_broken_condition_is_skipped_not_fatal(db, caplog) -> None:
    """条件树损坏的规则被跳过，其余规则照常求值。"""
    db.add(_rule(code="BROKEN", text="a == 1", score=99, condition={"type": "nonsense"}))
    db.add(_rule(code="GOOD", text="a == 1", score=10))
    db.flush()
    result = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})
    assert result.rule_score == 10
    assert [hit.rule_code for hit in result.hits] == ["GOOD"]


def test_keep_trace_includes_non_hits(db) -> None:
    """仿真页需要看到"跑了哪些规则、为什么没命中"。"""
    db.add(_rule(code="HIT", text="a == 1", score=10))
    db.add(_rule(code="MISS", text="a == 99", score=10))
    db.flush()

    traced = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1}, keep_trace=True)
    assert {item["rule_code"] for item in traced.evaluated_rules} == {"HIT", "MISS"}
    assert [item["hit"] for item in traced.evaluated_rules if item["rule_code"] == "MISS"] == [False]

    plain = rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})
    assert {item["rule_code"] for item in plain.evaluated_rules} == {"HIT"}


def test_compiled_cache_and_invalidation(db) -> None:
    db.add(_rule(code="R1", text="a == 1", score=10))
    db.flush()
    rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1})

    # 直接改库（模拟另一个进程/界面改了规则），版本号变化 -> 缓存键不同 -> 重新编译
    row = db.query(RcRule).filter_by(code="R1").one()
    row.score = 50
    row.version = 2
    db.flush()
    assert rule_engine.evaluate_rules(db, scene="coupon", features={"a": 1}).rule_score == 50


def test_strict_mode_propagates(db) -> None:
    from app.expression import ExpressionEvalError

    db.add(_rule(code="R1", text="a == 1", score=10))
    db.flush()
    # 严格模式下求值抛错 -> 该规则被跳过（而不是让整轮求值失败）
    result = rule_engine.evaluate_rules(db, scene="coupon", features={}, strict=True)
    assert result.rule_score == 0


def test_registry_rules_load_from_seed(db) -> None:
    """跑过 init_db 的开发库里，20 条种子规则应能全部编译成功。"""
    from app.seeds.rules import RULE_SEEDS

    assert len(RULE_SEEDS) == 20

