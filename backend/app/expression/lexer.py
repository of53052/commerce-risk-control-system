"""表达式词法分析。

把表达式文本切成 Token 流，交给 parser 组装 AST。

设计取舍：

1. **关键字不在这里判定**。``and`` / ``or`` / ``not`` / ``in`` / ``between`` 等
   一律先按标识符（ident）产出，由 parser 按上下文决定它是连接词还是算子。
   好处是 ``not_in`` 这类带下划线的算子名天然被标识符规则覆盖，
   不必在词法层维护一张"什么时候是关键字"的状态机。
2. **负数在词法层就吃掉**。本语言没有算术运算，``-`` 只可能出现在数字前，
   于是词法层直接产出负数 Token，parser 无需处理一元负号。
3. **单个 ``=`` 明确报错**。这是最常见的手误（把比较写成赋值），
   与其让 parser 报"意外的 Token"，不如在这里给出能直接照做的提示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.expression.errors import ExpressionSyntaxError


@dataclass(frozen=True)
class Token:
    """词法单元。``pos`` 为原始文本中的字符下标，用于报错定位。"""

    kind: str  # ident / number / string / op / lparen / rparen / lbracket / rbracket / comma / eof
    value: Any
    pos: int


# 多字符算子必须排在单字符前面，否则 ">=" 会被切成 ">" 再切成 "="。
_SYMBOL_OPS = ("==", "!=", ">=", "<=", "&&", "||", ">", "<", "!")

_PUNCT_KINDS = {
    "(": "lparen",
    ")": "rparen",
    "[": "lbracket",
    "]": "rbracket",
    ",": "comma",
}

# 标识符限定为 ASCII：避免中文变量名混进表达式后变成难以排查的"未知字段"。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 数字：整数或小数，允许前置负号。
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def tokenize(text: str) -> list[Token]:
    """把表达式文本切成 Token 列表，末尾追加 ``eof``。"""
    tokens: list[Token] = []
    i = 0
    length = len(text)

    while i < length:
        char = text[i]

        # 空白直接跳过：本语言对换行/缩进不敏感，前端可自由格式化表达式
        if char.isspace():
            i += 1
            continue

        # 字符串字面量：单双引号均可
        if char in ("'", '"'):
            i = _read_string(text, i, tokens)
            continue

        symbol = _match_symbol(text, i)
        if symbol is not None:
            tokens.append(Token("op", symbol, i))
            i += len(symbol)
            continue

        # 单独的 "=" 是赋值号，明确拦下并给出可照做的提示
        if char == "=":
            raise ExpressionSyntaxError("'=' 不是比较运算，请使用 '=='", i)

        kind = _PUNCT_KINDS.get(char)
        if kind is not None:
            tokens.append(Token(kind, char, i))
            i += 1
            continue

        # 数字（含负数）
        if char.isdigit() or (char == "-" and i + 1 < length and text[i + 1].isdigit()):
            match = _NUMBER_RE.match(text, i)
            if match is None:  # pragma: no cover - 上面的条件已保证能匹配
                raise ExpressionSyntaxError(f"无法解析数字：{text[i:i + 10]!r}", i)
            tokens.append(Token("number", _to_number(match.group(0)), i))
            i = match.end()
            continue

        # 标识符（true / false / null 也走这里，由 parser 解释为字面量）
        match = _IDENT_RE.match(text, i)
        if match is not None:
            tokens.append(Token("ident", match.group(0), i))
            i = match.end()
            continue

        raise ExpressionSyntaxError(f"无法识别的字符 {char!r}", i)

    tokens.append(Token("eof", None, length))
    return tokens


def _match_symbol(text: str, index: int) -> str | None:
    """返回 ``index`` 处匹配到的符号算子，无匹配返回 None。"""
    for symbol in _SYMBOL_OPS:
        if text.startswith(symbol, index):
            return symbol
    return None


def _to_number(literal: str) -> int | float:
    """数值字面量转 Python 数值：无小数点用 int，与 JSON 存储形态保持一致。"""
    return float(literal) if "." in literal else int(literal)


def _read_string(text: str, start: int, tokens: list[Token]) -> int:
    """读取一个字符串字面量，返回其结束后的下标。"""
    quote = text[start]
    i = start + 1
    chars: list[str] = []
    length = len(text)

    while i < length:
        char = text[i]
        if char == "\\" and i + 1 < length:
            nxt = text[i + 1]
            # 只对"引号与反斜杠本身"做转义还原；其余转义原样保留 ——
            # 正则里的 \d \w \. 必须原样交给 re，否则 matches 表达式会被改坏。
            if nxt in (quote, "\\"):
                chars.append(nxt)
                i += 2
                continue
            chars.append(char)
            i += 1
            continue
        if char == quote:
            tokens.append(Token("string", "".join(chars), start))
            return i + 1
        chars.append(char)
        i += 1

    raise ExpressionSyntaxError("字符串字面量未闭合", start)

