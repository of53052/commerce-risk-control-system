"""规则引擎：加载启用的规则、逐条求值、汇总分数与动作提示。

职责（docs/PRD.md §8.2）：
    1. 按 ``scene`` 取启用规则，按 ``priority`` 升序求值（数值越小越先算）；
    2. 用表达式引擎对每条规则的条件树求值，产出**逐叶子证据**；
    3. 累加命中规则的 ``score``；收集 ``action_hint=challenge`` 的显式声明；
    4. 一条规则在一次决策中最多计分一次（条件树里重复字段也只算一次）。

**为什么 Challenge 只能由规则显式声明**（docs/PRD.md §8.4）：
    若把某个分数区间隐式映射为 Challenge，策略师改一条规则的分数
    就可能把一些流量悄悄从"人工审核"挪到"二次验证"，而这件事在配置界面上
    完全看不出来。让 Challenge 必须由 ``action_hint`` 显式声明，
    策略意图就是可读、可审计的。

**规则缓存**：规则是低频变更、高频读取的数据。这里按 ``code@version`` 缓存
**编译后的 AST**（不是数据库行），进程内 LRU，规则写接口负责清缓存
（``invalidate_cache``）。缓存编译结果而非原始 JSON，
是因为解析与静态校验是每次决策都要付的成本，而规则内容几乎不变。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.expression import Node, evaluate, node_from_dict
from app.models.rule import ACTION_HINT_CHALLENGE, RcRule

logger = logging.getLogger("app.services.rule_engine")


@dataclass(frozen=True)
class CompiledRule:
    """编译后的规则（缓存单元）。"""

    code: str
    name: str
    scene: str
    category: str
    score: int
    action_hint: str
    priority: int
    version: int
    condition_text: str
    ast: Node
    cache_key: str


@dataclass(frozen=True)
class RuleHit:
    """一条规则的命中记录（落 rc_decision_hit）。"""

    rule_code: str
    rule_name: str
    rule_category: str
    score: int
    reason: str
    evidence: list[dict[str, Any]]
    missing_fields: list[str]
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "rule_name": self.rule_name,
            "rule_category": self.rule_category,
            "score": self.score,
            "reason": self.reason,
            "evidence": self.evidence,
            "missing_fields": self.missing_fields,
            "errors": self.errors,
        }


@dataclass
class RuleEvalResult:
    """一次决策的规则层结论。"""

    rule_score: int = 0
    hits: list[RuleHit] = field(default_factory=list)
    challenge_hint: bool = False
    evaluated: int = 0
    evaluated_rules: list[dict[str, Any]] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)


# 编译缓存：key = code@version
_compiled_cache: dict[str, CompiledRule] = {}


def invalidate_cache() -> None:
    """清空编译缓存。规则写接口在提交后调用。"""
    _compiled_cache.clear()


def _scene_filter(scene: str) -> list[str]:
    """场景过滤：``all`` 场景的规则对所有场景生效（通用规则）。"""
    return [scene, "all"] if scene != "all" else ["all"]


def load_rules(db: Session, *, scene: str, use_cache: bool = True) -> list[CompiledRule]:
    """加载并编译某场景下的启用规则，按 priority 升序。"""
    rows = (
        db.execute(
            select(RcRule)
            .where(RcRule.enabled.is_(True), RcRule.scene.in_(_scene_filter(scene)))
            .order_by(RcRule.priority.asc(), RcRule.id.asc())
        )
        .scalars()
        .all()
    )

    compiled: list[CompiledRule] = []
    for row in rows:
        cache_key = f"{row.code}@{row.version}"
        if use_cache and cache_key in _compiled_cache:
            compiled.append(_compiled_cache[cache_key])
            continue
        try:
            ast = node_from_dict(row.condition)
        except ValueError as exc:
            # 单条规则的条件树损坏，不能让整条决策链崩掉：
            # 跳过这条规则并记 ERROR 日志，让问题在日志里可见、在界面上可修。
            logger.error("规则 %s 的条件树无法解析，已跳过：%s", row.code, exc)
            continue
        item = CompiledRule(
            code=row.code,
            name=row.name,
            scene=row.scene,
            category=row.category,
            score=row.score,
            action_hint=row.action_hint,
            priority=row.priority,
            version=row.version,
            condition_text=row.condition_text,
            ast=ast,
            cache_key=cache_key,
        )
        if use_cache:
            _compiled_cache[cache_key] = item
        compiled.append(item)
    return compiled


def evaluate_rules(
    db: Session,
    *,
    scene: str,
    features: dict[str, Any],
    strict: bool = False,
    keep_trace: bool = False,
    use_cache: bool = True,
) -> RuleEvalResult:
    """对某场景的全部启用规则求值。

    ``keep_trace=True`` 时把**未命中**的规则也记入 ``evaluated_rules``：
    仿真页要展示"这条事件跑了哪些规则、为什么都没命中"，
    而常规决策为了控制决策表体积只落命中的规则。
    """
    result = RuleEvalResult()
    rules = load_rules(db, scene=scene, use_cache=use_cache)
    result.evaluated = len(rules)
    seen_fields: set[str] = set()

    for rule in rules:
        try:
            eval_result = evaluate(rule.ast, features, strict=strict)
        except Exception as exc:  # noqa: BLE001 - 单条规则异常不应中断整轮求值
            logger.error("规则 %s 求值异常，已跳过：%s", rule.code, exc)
            continue

        for field_name in eval_result.missing_fields:
            if field_name not in seen_fields:
                seen_fields.add(field_name)
                result.missing_fields.append(field_name)

        if eval_result.passed:
            hit = RuleHit(
                rule_code=rule.code,
                rule_name=rule.name,
                rule_category=rule.category,
                score=rule.score,
                reason=_build_reason(rule, eval_result),
                evidence=eval_result.evidence(),
                missing_fields=eval_result.missing_fields,
                errors=eval_result.errors,
            )
            result.hits.append(hit)
            # 一条规则一次决策只计分一次：这里是"按规则"循环，
            # 天生不会重复累加；条件树内部的重复字段由求值器去重。
            result.rule_score += rule.score
            if rule.action_hint == ACTION_HINT_CHALLENGE:
                result.challenge_hint = True

        if keep_trace or eval_result.passed:
            result.evaluated_rules.append(
                {
                    "rule_code": rule.code,
                    "rule_name": rule.name,
                    "category": rule.category,
                    "priority": rule.priority,
                    "score": rule.score,
                    "hit": eval_result.passed,
                    "condition_text": rule.condition_text,
                    "leaf_results": eval_result.evidence(),
                    "missing_fields": eval_result.missing_fields,
                    "errors": eval_result.errors,
                }
            )

    return result


def _build_reason(rule: CompiledRule, eval_result) -> str:
    """生成可读的触发原因。

    格式：``规则名：字段 比较 阈值（实际 X）``，最多列举 3 个成立的条件。
    审核员看的是"为什么拦我"，一句话要比一串 JSON 有用得多；
    完整证据链仍然落在 ``evidence`` 字段里备查。
    """
    parts: list[str] = []
    for leaf in eval_result.leaves:
        if not leaf.passed:
            continue
        parts.append(f"{leaf.field} {leaf.op} {_fmt(leaf.expected)}（实际 {_fmt(leaf.actual)}）")
        if len(parts) >= 3:
            break
    detail = "；".join(parts) if parts else "条件成立"
    suffix = f" 等 {len(eval_result.leaves)} 项条件" if len(eval_result.leaves) > 3 else ""
    return f"{rule.name}：{detail}{suffix}"


def _fmt(value: Any) -> str:
    """值格式化：字符串加引号，其余用 str。"""
    if isinstance(value, str):
        return f"'{value}'"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

