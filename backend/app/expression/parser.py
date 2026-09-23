"""表达式文本 ⇄ AST 双向转换。

语法（EBNF）::

    expr        := or_expr
    or_expr     := and_expr (("or" | "||") and_expr)*
    and_expr    := unary   (("and" | "&&") unary)*
    unary       := ("not" | "!") unary | primary
    primary     := "(" expr ")" | condition
    condition   := IDENT OP operand
    OP          := ">" | ">=" | "<" | "<=" | "==" | "!=" | "in" | "not_in"
                 | "between" | "contains" | "matches"
    operand     := number | string | "true" | "false" | "null" | "[" list "]"

优先级：``not`` > ``and`` > ``or``（与直觉一致；需要别的顺序就加括号）。
全部左结合。``in`` / ``not_in`` / ``between`` 的右操作数可以是方括号列表
（``[1, 2, 3]``）也可以直接跟字面量列表（``1, 2, 3``），两种写法等价 ——
前者更清晰，后者更省事，都给。

``render`` 是 ``parse`` 的逆运算，用于把条件树编辑器的结果回显成表达式文本。
两者互逆是有测试保障的硬约束（见 backend/tests/test_expression_parser.py）：
``parse(render(ast))`` 与 ``ast`` **语义等价**；对由 ``parse`` 产出的树（同算子链是扁平的）
则结构完全相同。手写构造的 ``And(And(a, b), c)`` 会被展平成 ``And(a, b, c)`` ——
语义不变，这是刻意接受的归一化。
"""

from __future__ import annotations

from typing import Any

from app.expression.ast import And, Condition, Node, Not, Or, SUPPORTED_OPS
from app.expression.errors import ExpressionSyntaxError
from app.expression.lexer import Token, tokenize

# 文本连接词 -> 内部节点类型
_AND_WORDS = {"and", "&&"}
_OR_WORDS = {"or", "||"}
_NOT_WORDS = {"not", "!"}

# 文本算子别名：把符号写法归一成 AST 里的标准算子名
_OP_ALIASES = {
    "==": "==",
    "!=": "!=",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
}

_BOOL_LITERALS = {"true": True, "false": False}
_NULL_LITERALS = {"null", "none"}


class _Parser:
    """递归下降解析器（Token 流 -> AST）。"""

    def __init__(self, tokens: list[Token], text: str) -> None:
        self._tokens = tokens
        self._text = text
        self._index = 0

    # ---- Token 游标 ----
    @property
    def _current(self) -> Token:
        return self._tokens[self._index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        if token.kind != "eof":
            self._index += 1
        return token

    def _is_word(self, token: Token, words: set[str]) -> bool:
        return token.kind == "ident" and str(token.value).lower() in words

    def _is_symbol(self, token: Token, symbols: set[str]) -> bool:
        return token.kind == "op" and token.value in symbols

    # ---- 语法规则 ----
    def parse(self) -> Node:
        node = self._parse_or()
        if self._current.kind != "eof":
            raise ExpressionSyntaxError(
                f"表达式在 {self._current.value!r} 处有无法解析的剩余内容",
                self._current.pos,
            )
        return node

    def _parse_or(self) -> Node:
        children = [self._parse_and()]
        while self._is_word(self._current, _OR_WORDS) or self._is_symbol(self._current, _OR_WORDS):
            self._advance()
            children.append(self._parse_and())
        return children[0] if len(children) == 1 else Or(children=tuple(children))

    def _parse_and(self) -> Node:
        children = [self._parse_unary()]
        while self._is_word(self._current, _AND_WORDS) or self._is_symbol(self._current, _AND_WORDS):
            self._advance()
            children.append(self._parse_unary())
        return children[0] if len(children) == 1 else And(children=tuple(children))

    def _parse_unary(self) -> Node:
        if self._is_word(self._current, _NOT_WORDS) or self._is_symbol(self._current, _NOT_WORDS):
            self._advance()
            return Not(child=self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> Node:
        token = self._current
        if token.kind == "lparen":
            self._advance()
            node = self._parse_or()
            if self._current.kind != "rparen":
                raise ExpressionSyntaxError("括号未闭合", token.pos)
            self._advance()
            return node
        return self._parse_condition()

    def _parse_condition(self) -> Node:
        field_token = self._current
        if field_token.kind != "ident":
            raise ExpressionSyntaxError(
                f"期望特征字段名，实际是 {field_token.value!r}", field_token.pos
            )
        field = str(field_token.value)
        self._advance()

        op = self._parse_operator()
        value = self._parse_operand(op)
        return Condition(field=field, op=op, value=value)

    def _parse_operator(self) -> str:
        token = self._current
        if token.kind == "op" and token.value in _OP_ALIASES:
            self._advance()
            return _OP_ALIASES[token.value]
        if token.kind == "ident":
            word = str(token.value).lower()
            if word in SUPPORTED_OPS:
                self._advance()
                return word
            if word == "not":
                # 允许 "not in" 这种带空格的写法（更接近自然语言）
                self._advance()
                if str(self._current.value).lower() == "in":
                    self._advance()
                    return "not_in"
                raise ExpressionSyntaxError("'not' 只能作为 'not in' 或取反连接词使用", token.pos)
        raise ExpressionSyntaxError(
            f"期望比较算子（{'/'.join(sorted(SUPPORTED_OPS))}），实际是 {token.value!r}",
            token.pos,
        )

    def _parse_operand(self, op: str) -> Any:
        token = self._current

        # 列表写法：[1, 2, 3]
        if token.kind == "lbracket":
            self._advance()
            items: list[Any] = []
            if self._current.kind != "rbracket":
                # 方括号内只允许标量：这里若递归调用 _parse_operand，会触发下面
                # 的"裸列表收集"分支，把 [0.5, 1.0] 整体当成一个元素，
                # 得到 [[0.5, 1.0]] —— 这正是 render 往返测试抓到的真实 bug。
                items.append(self._parse_scalar())
                while self._current.kind == "comma":
                    self._advance()
                    items.append(self._parse_scalar())
            if self._current.kind != "rbracket":
                raise ExpressionSyntaxError("方括号未闭合", token.pos)
            self._advance()
            return items

        value = self._parse_scalar()

        # 裸列表写法：1, 2, 3（仅个别算子需要，且只在确实出现逗号时才收集，
        # 避免把 "a in 1" 这种单值写法误判成列表）
        if op in {"in", "not_in", "between"} and self._current.kind == "comma":
            items = [value]
            while self._current.kind == "comma":
                self._advance()
                items.append(self._parse_scalar())
            return items
        return value

    def _parse_scalar(self) -> Any:
        token = self._current
        if token.kind in {"number", "string"}:
            self._advance()
            return token.value
        if token.kind == "ident":
            word = str(token.value).lower()
            if word in _BOOL_LITERALS:
                self._advance()
                return _BOOL_LITERALS[word]
            if word in _NULL_LITERALS:
                self._advance()
                return None
        raise ExpressionSyntaxError(f"期望字面量，实际是 {token.value!r}", token.pos)


def parse(text: str) -> Node:
    """表达式文本 -> AST。语法错误抛 ``ExpressionSyntaxError``（含字符位置）。"""
    if not isinstance(text, str) or not text.strip():
        raise ExpressionSyntaxError("表达式不能为空", 0)
    return _Parser(tokenize(text), text).parse()


# --------------------------------------------------------------------------- #
# AST -> 文本
# --------------------------------------------------------------------------- #
_OP_TO_TEXT = {
    "==": "==",
    "!=": "!=",
    ">": ">",
    ">=": ">=",
    "<": "<",
    "<=": "<=",
    "in": "in",
    "not_in": "not_in",
    "between": "between",
    "contains": "contains",
    "matches": "matches",
}


def _literal_to_text(value: Any) -> str:
    """把字面量渲染成可被 ``parse`` 读回的文本。

    字符串一律用单引号并转义内部单引号与反斜杠；
    列表渲染为 ``[a, b, c]``，保证嵌套结构不会因为缺括号而产生歧义。
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_literal_to_text(item) for item in value) + "]"
    raise ExpressionSyntaxError(f"无法渲染的操作数类型：{type(value).__name__}")


def render(node: Node, _parent_precedence: int = 0) -> str:
    """AST -> 表达式文本。

    按优先级决定是否补括号：``or``(1) < ``and``(2) < ``not``(3) < ``condition``(4)。
    父节点优先级高于子节点时才加括号。这样 ``a and (b or c)`` 不会被渲染成
    ``a and b or c`` —— 后者语义完全不同，是最危险的"渲染丢语义"事故。
    """
    precedence = _node_precedence(node)

    if isinstance(node, Condition):
        text = f"{node.field} {_OP_TO_TEXT[node.op]} {_literal_to_text(node.value)}"
    elif isinstance(node, And):
        text = " and ".join(render(child, precedence) for child in node.children)
    elif isinstance(node, Or):
        text = " or ".join(render(child, precedence) for child in node.children)
    else:  # Not
        text = f"not {render(node.child, precedence)}"

    if precedence < _parent_precedence:
        return f"({text})"
    return text


def _node_precedence(node: Node) -> int:
    if isinstance(node, Or):
        return 1
    if isinstance(node, And):
        return 2
    if isinstance(node, Not):
        return 3
    return 4
