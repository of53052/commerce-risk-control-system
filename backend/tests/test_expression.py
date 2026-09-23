"""表达式引擎测试。

覆盖的真实风险（每条都对应一个曾经踩到或极易踩到的坑）：
    1. 双模编辑的往返一致性：条件树 ⇄ 表达式文本来回切换时不能丢结构；
    2. 括号与优先级：``a and (b or c)`` 渲染时若丢括号，语义会静默反转；
    3. 列表操作数只包一层方括号（曾因递归解析导致 ``[[0.5, 1.0]]``）；
    4. 字段缺失判 False 并留痕，而不是抛异常打挂整条决策链；
    5. 严格模式下必须显式抛错，否则"规则静默失效"无法在联调期暴露；
    6. 单个 ``=`` / 未闭合括号 / 未闭合字符串要给出可定位的报错位置。
"""

from __future__ import annotations

import pytest

from app.expression import (
    And,
    Condition,
    ExpressionEvalError,
    ExpressionSyntaxError,
    Not,
    Or,
    evaluate,
    node_from_dict,
    node_to_dict,
    parse,
    render,
    validate_node,
)


# --------------------------------------------------------------------------- #
# 解析与渲染往返
# --------------------------------------------------------------------------- #
ROUND_TRIP_CASES = [
    "device_account_cnt_24h >= 3",
    "device_account_cnt_24h >= 3 and user_coupon_cnt_1h > 5 and account_age_days < 7",
    "not (ip_is_datacenter == true) or address_phone_share_cnt_7d >= 3",
    "user_refund_rate_24h between [0.5, 1.0] and user_refund_amount_24h > 2000",
    "ip_region in ['HK', 'SG'] and device_env_risk not_in [0]",
    r"biz_no matches '^RF\d{8}$' and order_remark contains 'urgent'",
    "a == 1 and (b == 2 or c == 3) and not d == 4",
    "x between 1, 2",
    "user_refund_cnt_7d >= 8 or (user_order_cnt_24h == 0 and user_coupon_cnt_24h >= 5)",
    "not (a == 1 and b == 2)",
    "flag == false or flag == true",
]


@pytest.mark.parametrize("text", ROUND_TRIP_CASES)
def test_parse_render_round_trip(text: str) -> None:
    """parse → render → parse 后 AST 必须完全一致。"""
    node = parse(text)
    assert node_to_dict(parse(render(node))) == node_to_dict(node)


def test_render_keeps_parentheses_for_or_inside_and() -> None:
    """``a and (b or c)`` 不能渲染成 ``a and b or c``（语义完全不同）。"""
    node = parse("a == 1 and (b == 2 or c == 3)")
    assert "(" in render(node)
    assert node_to_dict(parse(render(node))) == node_to_dict(node)


def test_not_inside_and_is_parenthesized_when_needed() -> None:
    node = Not(child=And(children=(Condition("a", "==", 1), Condition("b", "==", 2))))
    text = render(node)
    assert text == "not (a == 1 and b == 2)"
    assert node_to_dict(parse(text)) == node_to_dict(node)


def test_same_operator_chain_is_flattened_not_nested() -> None:
    """同算子链天然扁平：手写的嵌套 And 会被归一化（语义不变）。"""
    nested = And(children=(And(children=(Condition("a", "==", 1), Condition("b", "==", 2))), Condition("c", "==", 3)))
    assert render(nested) == "a == 1 and b == 2 and c == 3"
    assert node_to_dict(parse(render(nested))) == node_to_dict(And(children=(Condition("a", "==", 1), Condition("b", "==", 2), Condition("c", "==", 3))))


def test_or_binds_looser_than_and() -> None:
    node = parse("a == 1 and b == 2 or c == 3")
    assert isinstance(node, Or)
    assert isinstance(node.children[0], And)


def test_symbol_aliases() -> None:
    """``&&`` / ``||`` / ``!`` 与 ``and`` / ``or`` / ``not`` 等价。"""
    assert node_to_dict(parse("a == 1 && b == 2")) == node_to_dict(parse("a == 1 and b == 2"))
    assert node_to_dict(parse("a == 1 || b == 2")) == node_to_dict(parse("a == 1 or b == 2"))
    assert node_to_dict(parse("!(a == 1)")) == node_to_dict(parse("not (a == 1)"))


def test_not_in_spaced_form() -> None:
    """``not in`` 与 ``not_in`` 等价。"""
    assert node_to_dict(parse("a not in [1, 2]")) == node_to_dict(parse("a not_in [1, 2]"))


# --------------------------------------------------------------------------- #
# 语法错误与定位
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("a = 1", "请使用 '=='"),
        ("a == ", "期望字面量"),
        ("(a == 1", "括号未闭合"),
        ("a == 1 and", "期望特征字段名"),
        ("a foo 1", "期望比较算子"),
        ("a == 'unclosed", "未闭合"),
        ("a == [1, 2", "方括号未闭合"),
        ("", "不能为空"),
    ],
)
def test_syntax_errors_are_actionable(text: str, keyword: str) -> None:
    with pytest.raises(ExpressionSyntaxError) as exc:
        parse(text)
    assert keyword in str(exc.value)


# --------------------------------------------------------------------------- #
# 求值
# --------------------------------------------------------------------------- #
def test_missing_field_is_false_and_recorded() -> None:
    node = parse("device_account_cnt_24h >= 3 and user_coupon_cnt_1h > 5 and account_age_days < 7")
    result = evaluate(node, {"device_account_cnt_24h": 4, "user_coupon_cnt_1h": 9})
    assert result.passed is False
    assert result.missing_fields == ["account_age_days"]
    assert result.errors == []


@pytest.mark.parametrize(
    ("text", "features", "expected"),
    [
        ("a == 1", {"a": 1}, True),
        ("a == 1", {"a": 1.0}, True),          # 数值比较不区分 int/float
        ("a == 1", {"a": "1"}, False),          # 不做字符串到数字的隐式猜测
        ("a != 1", {"a": "1"}, True),
        ("a > 2", {"a": 3}, True),
        ("a between [1, 3]", {"a": 3}, True),
        ("a between [1, 3]", {"a": 4}, False),
        ("a in [1, 2]", {"a": 2}, True),
        ("a not_in [1, 2]", {"a": 2}, False),
        ("a contains 'bc'", {"a": "abcd"}, True),
        ("a contains 'z'", {"a": ["x", "y"]}, False),
        ("a contains 'y'", {"a": ["x", "y"]}, True),
        (r"a matches '^RF\d{8}$'", {"a": "RF12345678"}, True),
        (r"a matches '^RF\d{8}$'", {"a": "RF123"}, False),
        ("flag == true", {"flag": True}, True),
        ("flag == 1", {"flag": True}, True),     # 布尔放宽为 0/1，名单标记类特征常见
        ("a == null", {"a": 1}, False),
    ],
)
def test_compare_semantics(text: str, features: dict, expected: bool) -> None:
    assert evaluate(parse(text), features).passed is expected


def test_none_value_is_treated_as_missing() -> None:
    """特征引擎写 None 表示"算不出来"，与键不存在应等价处理。"""
    result = evaluate(parse("a == 1"), {"a": None})
    assert result.passed is False
    assert result.missing_fields == ["a"]


def test_type_mismatch_records_error_without_raising() -> None:
    result = evaluate(parse("a > 3"), {"a": "HK"})
    assert result.passed is False
    assert result.errors and "类型不可比" in result.errors[0]


def test_evidence_covers_every_leaf_without_short_circuit() -> None:
    """不短路：value 已确定为 False 时，后续条件仍要出现在证据链里。"""
    node = parse("a == 1 and b == 2 and c == 3")
    result = evaluate(node, {"a": 0, "b": 2, "c": 3})
    assert [leaf.field for leaf in result.leaves] == ["a", "b", "c"]
    assert [leaf.passed for leaf in result.leaves] == [False, True, True]


def test_or_still_evaluates_all_leaves() -> None:
    result = evaluate(parse("a == 1 or b == 2"), {"a": 1, "b": 2})
    assert result.passed is True
    assert len(result.leaves) == 2


def test_strict_mode_raises() -> None:
    with pytest.raises(ExpressionEvalError):
        evaluate(parse("a == 1"), {}, strict=True)


def test_not_with_missing_field_stays_false() -> None:
    """缺失判 False 后再取反会变成 True —— 这是刻意的语义，必须被记录在案。

    说明：``not (a == 1)`` 在 a 缺失时结果为 True。这符合"该条件不成立"的直接推论，
    但策略师写 ``not`` 时必须意识到这一点。此处用测试把语义钉死，避免日后被人
    当成 bug 改掉（改掉反而会让"缺失"变成一个三值逻辑，复杂度陡增）。
    """
    result = evaluate(parse("not (a == 1)"), {})
    assert result.passed is True
    assert result.missing_fields == ["a"]


# --------------------------------------------------------------------------- #
# JSON 序列化与校验
# --------------------------------------------------------------------------- #
def test_node_json_round_trip() -> None:
    node = parse("a == 1 and (b >= 2 or not c == 3)")
    assert node_to_dict(node_from_dict(node_to_dict(node))) == node_to_dict(node)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"type": "unknown"},
        {"type": "condition", "op": "==", "value": 1},          # 缺 field
        {"type": "condition", "field": "a", "op": "===", "value": 1},  # 不支持算子
        {"type": "condition", "field": "a", "op": "=="},         # 缺 value
        {"type": "and", "children": []},                          # 空 children
        {"type": "not"},                                          # 缺 child
    ],
)
def test_node_from_dict_rejects_bad_shapes(payload: object) -> None:
    with pytest.raises(ValueError):
        node_from_dict(payload)


def test_validate_node_flags_unknown_field() -> None:
    known = {"a", "b"}
    assert validate_node(parse("a == 1 and b == 2"), known) == []
    problems = validate_node(parse("a == 1 and zz == 2"), known)
    assert any("zz" in item for item in problems)


def test_validate_node_checks_operand_shapes() -> None:
    assert validate_node(parse("a between [1, 2]")) == []
    assert validate_node(Condition("a", "between", [1, 2, 3]))
    assert validate_node(Condition("a", "in", 1))
    assert validate_node(Condition("a", "matches", "(unclosed"))

