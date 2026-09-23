r"""规则条件求值器（白名单式，无 ``eval``）。

三条关键语义（docs/PRD.md §8.2 的落地）：

1. **字段缺失判 False + 记入 missing_fields**，而不是抛异常。
   原因：风控热路径上"某个特征这次算不出来"是常态（新设备、无历史订单），
   为此炸掉整条决策链是把可用性问题放大成故障。
   需要"缺失即报错"的场景由 ``strict=True`` 提供（对应 ``sys_config.rule_strict_mode``）。
2. **不做短路求值**。``and``/``or`` 一律把全部叶子都求一遍。
   反直觉但刻意：本模块的产物是**证据链** —— 审核员需要看到
   "5 个条件里这 3 个成立、那 1 个字段这次没数据"，
   短路会让证据链随数据变化而缺项，现场问答时说不清。
   代价是极端深树会多算一些条件，而条件树有 50 叶子上限（ast.validate_node）兜底。
3. **None 视为缺失**。特征引擎对"算不出来"的特征写入 None，
   与"这个键不存在"在业务上是同一件事，统一按缺失处理，避免两套分支。

类型比较策略：
    - 数值算子（>, >=, <, <=）要求两侧均为数值，否则记错误并判 False；
    - ``==`` / ``!=`` 两侧都是数值时按数值比较（``1 == 1.0`` 为真），
      否则按 Python 原生比较（``'1' != 1`` 为真，不做字符串转数字的隐式猜测 ——
      隐式转换会让"字段类型被改坏"变成静默的错误结论）；
    - ``matches`` 用 ``re.search``（子串语义，符合"正则匹配"的直觉；``fullmatch``
      会让策略师写 ``\d+`` 时意外不匹配）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.expression.ast import And, Condition, Node, Not, Or
from app.expression.errors import ExpressionEvalError

# 数值算子：两侧都必须是数值
_NUMERIC_OPS = frozenset({">", ">=", "<", "<="})


@dataclass(frozen=True)
class LeafResult:
    """单个条件的求值结果 —— 证据链的最小单元。"""

    field: str
    op: str
    expected: Any
    actual: Any
    passed: bool
    missing: bool = False
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成可落 ``rc_decision_hit.evidence`` 的 JSON 结构。"""
        return {
            "field": self.field,
            "op": self.op,
            "expected": self.expected,
            "actual": self.actual,
            "passed": self.passed,
            "missing": self.missing,
            "detail": self.detail,
        }


@dataclass
class EvalResult:
    """整条规则条件的求值结果。"""

    passed: bool
    leaves: list[LeafResult] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def evidence(self) -> list[dict[str, Any]]:
        return [leaf.to_dict() for leaf in self.leaves]


def evaluate(
    node: Node,
    features: Mapping[str, Any],
    *,
    strict: bool = False,
) -> EvalResult:
    """求值。

    ``strict=True`` 时，字段缺失或类型不可比会抛 ``ExpressionEvalError``，
    用于策略联调：把"规则悄悄失效"变成显式报错。
    """
    result = EvalResult(passed=False)
    result.passed = _eval(node, features, result)

    if strict and (result.missing_fields or result.errors):
        detail = result.errors or [f"缺失字段：{f}" for f in result.missing_fields]
        raise ExpressionEvalError("；".join(detail))

    return result


def _eval(node: Node, features: Mapping[str, Any], result: EvalResult) -> bool:
    """递归求值。注意：内部不短路，见模块文档第 2 条。"""
    if isinstance(node, Condition):
        return _eval_condition(node, features, result)

    if isinstance(node, Or):
        # 先全部求值再聚合：or 的全量求值天然不会影响结果正确性
        values = [_eval(child, features, result) for child in node.children]
        return any(values)

    if isinstance(node, And):
        values = [_eval(child, features, result) for child in node.children]
        return all(values)

    # Not
    return not _eval(node.child, features, result)


def _eval_condition(
    node: Condition,
    features: Mapping[str, Any],
    result: EvalResult,
) -> bool:
    actual = features.get(node.field)

    # 缺失（键不存在或值为 None）：判 False 并留痕
    if node.field not in features or actual is None:
        if node.field not in result.missing_fields:
            result.missing_fields.append(node.field)
        result.leaves.append(
            LeafResult(
                field=node.field,
                op=node.op,
                expected=node.value,
                actual=None,
                passed=False,
                missing=True,
                detail="特征缺失，该条件判为不成立",
            )
        )
        return False

    passed, error = _compare(node.op, actual, node.value)
    if error is not None and error not in result.errors:
        result.errors.append(error)

    result.leaves.append(
        LeafResult(
            field=node.field,
            op=node.op,
            expected=node.value,
            actual=actual,
            passed=passed,
            detail=error,
        )
    )
    return passed


def _is_number(value: Any) -> bool:
    """bool 也放宽为数值：名单/标记类特征常以 true/false 存储，等价于 1/0。"""
    return isinstance(value, (int, float))


def _compare(op: str, actual: Any, expected: Any) -> tuple[bool, str | None]:
    """执行一次比较，返回 (是否成立, 错误说明)。"""
    try:
        if op in _NUMERIC_OPS:
            if not (_is_number(actual) and _is_number(expected)):
                return False, f"数值算子 {op} 的类型不可比：实际 {type(actual).__name__}，期望 {type(expected).__name__}"
            if op == ">":
                return actual > expected, None
            if op == ">=":
                return actual >= expected, None
            if op == "<":
                return actual < expected, None
            return actual <= expected, None

        if op == "==":
            return _equal(actual, expected), None
        if op == "!=":
            return not _equal(actual, expected), None

        if op in {"in", "not_in"}:
            if not isinstance(expected, (list, tuple, set)):
                return False, f"算子 {op} 的操作数应为列表，实际为 {type(expected).__name__}"
            hit = any(_equal(actual, item) for item in expected)
            return (hit if op == "in" else not hit), None

        if op == "between":
            if not isinstance(expected, (list, tuple)) or len(expected) != 2:
                return False, "between 的操作数应为 [下界, 上界]"
            low, high = expected
            if not (_is_number(actual) and _is_number(low) and _is_number(high)):
                return False, "between 的类型不可比：实际值与上下界必须都是数值"
            return (low <= actual <= high), None

        if op == "contains":
            if isinstance(actual, str):
                return str(expected) in actual, None
            if isinstance(actual, (list, tuple, set, dict)):
                return expected in actual, None
            return False, f"contains 不适用于 {type(actual).__name__} 类型"

        if op == "matches":
            if not isinstance(expected, str):
                return False, "matches 的操作数应为正则字符串"
            return _compile_regex(expected).search(str(actual)) is not None, None

        return False, f"未知算子：{op}"
    except re.error as exc:
        return False, f"正则执行失败：{exc}"


def _equal(left: Any, right: Any) -> bool:
    """相等判断：两侧都是数值时按数值比较，否则原样比较。"""
    if _is_number(left) and _is_number(right):
        return float(left) == float(right)
    return left == right


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> re.Pattern[str]:
    """正则编译缓存。

    规则集是有限且稳定的（几十条），每条规则的正则最多编译一次；
    缓存上限 256 足以覆盖全部规则，同时避免"用户随手输入的正则"撑爆内存。
    """
    return re.compile(pattern)
