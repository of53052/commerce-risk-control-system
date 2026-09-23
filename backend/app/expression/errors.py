"""表达式引擎的异常类型。

单独放一个模块是为了打断循环依赖：lexer 需要抛语法错误，parser 需要捕获并补充上下文，
而 parser 又要 import lexer。异常定义放在两者之外，双方都只依赖它。
"""

from __future__ import annotations


class ExpressionSyntaxError(ValueError):
    """表达式文本语法错误。

    带 ``pos``（字符下标）是刻意的：策略师在配置页写错表达式时，
    接口要能把"第几个字符出错"直接回显给前端高亮，
    否则用户只能对着整行文本猜哪里写错了。
    """

    def __init__(self, message: str, pos: int | None = None) -> None:
        self.pos = pos
        suffix = f"（位置 {pos}）" if pos is not None else ""
        super().__init__(f"{message}{suffix}")


class ExpressionEvalError(RuntimeError):
    """求值期错误。

    默认（宽容模式）下不抛本异常：字段缺失、类型不匹配只记入 ``EvalResult.errors``
    并让该条件判为 False。只有 ``strict=True``（``sys_config: rule_strict_mode``）
    才抛出，用于策略联调阶段把问题暴露成显式失败。
    """

