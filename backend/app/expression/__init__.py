"""规则条件表达式引擎（自研，白名单式，无 ``eval``）。

对外只暴露四件事：
    - ``Node`` 及其子类（``ast`` 模块）+ ``node_from_dict`` / ``node_to_dict``
    - ``parse(text) -> Node``（表达式文本 → AST）
    - ``render(node) -> str``（AST → 表达式文本，与 parse 互逆）
    - ``evaluate(node, features, ...) -> EvalResult``（AST + 特征 → 布尔 + 证据）

为什么自研而不用现成方案（docs/PRD.md §8.2）：
    规则条件要同时支持「条件树」与「表达式文本」两种编辑形态，且必须能逐条回放
    "哪些条件成立、哪些字段缺失"作为证据链。通用表达式库（如 asteval / simpleeval）
    要么允许函数调用（白名单收不紧），要么不提供逐叶子求值证据。
    自研后，AST 是 JSON 可存储的纯数据，字段白名单与算子集合完全可控。

安全前提：整条链路上没有 ``eval`` / ``exec`` / 属性访问 / 函数调用，
表达式只是「字段名 + 算子 + 字面量」的组合，最坏情况是求值报错。
"""

from app.expression.ast import (
    And,
    Condition,
    Node,
    Not,
    Or,
    collect_fields,
    node_from_dict,
    node_to_dict,
    validate_node,
)
from app.expression.errors import ExpressionEvalError
from app.expression.evaluator import EvalResult, LeafResult, evaluate
from app.expression.parser import ExpressionSyntaxError, parse, render

__all__ = [
    "And",
    "Condition",
    "EvalResult",
    "ExpressionEvalError",
    "ExpressionSyntaxError",
    "LeafResult",
    "Node",
    "Not",
    "Or",
    "collect_fields",
    "evaluate",
    "node_from_dict",
    "node_to_dict",
    "parse",
    "render",
    "validate_node",
]
