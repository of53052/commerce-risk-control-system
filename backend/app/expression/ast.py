"""条件 AST 定义。

设计要点：

1. **AST 是纯数据**：每个节点都能无损序列化为 JSON（存 ``rc_rule.condition``），
   也能从 JSON 还原。前端条件树编辑器直接读写这份 JSON，不需要另立格式。
2. **四种节点，穷尽表达力**：``and`` / ``or`` / ``not`` / ``condition``。
   刻意不做 ``xor``、算术运算、函数调用 —— 风控规则用不上，
   而且每多一类节点，求值器与前端编辑器都要各多一套分支。
3. **不可变（frozen）**：节点一旦构造就不再修改，避免"同一棵 AST 被两处引用、
   一处改了另一处跟着变"的隐蔽 bug。修改规则 = 造一棵新树 + 版本留痕。

JSON 形态（即 ``rc_rule.condition`` 的存储结构）::

    {"type": "and", "children": [
        {"type": "condition", "field": "device_account_cnt_24h", "op": ">=", "value": 3},
        {"type": "not", "child": {"type": "condition", ...}}
    ]}
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# 允许的算子集合（docs/PRD.md §8.2）。新增算子必须同时改三处：
# 本集合、parser 的词法/语法、evaluator 的 _compare。
SUPPORTED_OPS: frozenset[str] = frozenset(
    {">", ">=", "<", "<=", "==", "!=", "in", "not_in", "between", "contains", "matches"}
)

# 需要「列表型」操作数的算子：between 需 [lo, hi]，in/not_in 需候选值列表。
LIST_OPERAND_OPS: frozenset[str] = frozenset({"in", "not_in", "between"})

# 需要「正则字符串」操作数的算子。
REGEX_OPERAND_OPS: frozenset[str] = frozenset({"matches"})


class Node:
    """节点基类。仅用于类型标注与统一接口，不直接实例化。"""

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - 由子类实现
        raise NotImplementedError


@dataclass(frozen=True)
class Condition(Node):
    """叶子节点：单条件判断。"""

    field: str
    op: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {"type": "condition", "field": self.field, "op": self.op, "value": self.value}


@dataclass(frozen=True)
class And(Node):
    """全部子节点为真才为真。"""

    children: tuple[Node, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "and", "children": [c.to_dict() for c in self.children]}


@dataclass(frozen=True)
class Or(Node):
    """任一子节点为真即为真。"""

    children: tuple[Node, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"type": "or", "children": [c.to_dict() for c in self.children]}


@dataclass(frozen=True)
class Not(Node):
    """取反。"""

    child: Node

    def to_dict(self) -> dict[str, Any]:
        return {"type": "not", "child": self.child.to_dict()}


def node_to_dict(node: Node) -> dict[str, Any]:
    """节点 -> JSON 可存储的 dict。"""
    return node.to_dict()


def node_from_dict(data: Any) -> Node:
    """dict -> 节点。结构非法时抛 ``ValueError``（调用方负责转成接口错误）。

    这里对所有形状问题都显式报错，而不是宽容地把未知结构当成 ``None``：
    规则是"策略源"，静默丢条件等于静默放宽风控，属于最危险的一类故障。
    """
    if not isinstance(data, dict):
        raise ValueError(f"条件节点必须是对象，实际为 {type(data).__name__}")

    node_type = data.get("type")
    if node_type == "condition":
        field = data.get("field")
        op = data.get("op")
        if not isinstance(field, str) or not field:
            raise ValueError("condition 节点缺少合法的 field")
        if op not in SUPPORTED_OPS:
            raise ValueError(f"condition 节点使用了不支持的算子：{op!r}")
        if "value" not in data:
            raise ValueError("condition 节点缺少 value")
        return Condition(field=field, op=op, value=data["value"])

    if node_type in {"and", "or"}:
        children = data.get("children")
        if not isinstance(children, list) or not children:
            raise ValueError(f"{node_type} 节点必须包含非空的 children 列表")
        parsed = tuple(node_from_dict(child) for child in children)
        return And(children=parsed) if node_type == "and" else Or(children=parsed)

    if node_type == "not":
        if "child" not in data:
            raise ValueError("not 节点缺少 child")
        return Not(child=node_from_dict(data["child"]))

    raise ValueError(f"未知的节点类型：{node_type!r}")


def collect_fields(node: Node) -> set[str]:
    """收集整棵树引用到的字段名。

    用途：规则保存前做白名单校验、以及"这条规则依赖哪些特征"的影响面分析。
    """
    if isinstance(node, Condition):
        return {node.field}
    if isinstance(node, (And, Or)):
        result: set[str] = set()
        for child in node.children:
            result |= collect_fields(child)
        return result
    return collect_fields(node.child)


def validate_node(
    node: Node,
    known_fields: frozenset[str] | set[str] | None = None,
    max_leaves: int = 50,
) -> list[str]:
    """结构 + 字段白名单 + 操作数形态校验，返回问题列表（空列表 = 通过）。

    为什么在"保存规则"时就校验，而不是等到决策时才发现：
        一条引用了不存在字段的规则，求值结果恒为 false（宽容模式），
        表面看"系统正常"，实际是策略静默失效 —— 这类问题必须在上线前拦住。

    ``max_leaves`` 是防呆上限：条件树被前端误操作展开成几百个节点时，
    单次决策的求值耗时会线性膨胀，宁可保存时报错也不要拖垮热路径。
    """
    problems: list[str] = []

    if isinstance(node, Condition):
        if known_fields is not None and node.field not in known_fields:
            problems.append(f"未知特征字段：{node.field}")
        if node.op in LIST_OPERAND_OPS:
            if not isinstance(node.value, list) or not node.value:
                problems.append(f"算子 {node.op} 的操作数必须是非空列表")
            elif node.op == "between" and len(node.value) != 2:
                problems.append("between 的操作数必须是 [下界, 上界] 两个元素")
        if node.op in REGEX_OPERAND_OPS:
            if not isinstance(node.value, str):
                problems.append("matches 的操作数必须是正则字符串")
            else:
                try:
                    re.compile(node.value)
                except re.error as exc:
                    # 正则在保存时编译一次：运行期每次决策都编译是纯浪费，
                    # 而且写错的正在保存时就该报出来。
                    problems.append(f"matches 正则不合法：{exc}")
        return problems

    if isinstance(node, (And, Or)):
        children = node.children
        kind = "and" if isinstance(node, And) else "or"
        if not children:
            problems.append(f"{kind} 节点不能为空")
        for child in children:
            problems.extend(validate_node(child, known_fields, max_leaves))
        return problems

    # Not
    return validate_node(node.child, known_fields, max_leaves)
