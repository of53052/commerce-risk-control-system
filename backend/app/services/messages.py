"""领域消息文案（决策原因、校验失败原因）。

**为什么集中在一个模块里**：这些字符串会同时出现在三个地方 —— 接口响应、
审核工作台界面、审计与排障日志。散落在各处写死，必然出现
「日志里写 system_error、响应里写 sys_error」这类口径分裂，
而排障时按关键字 grep 不到是最浪费时间的。

这里的文案是**面向使用者的自然语言**（审核员、策略师），不是面向机器的错误码；
机器错误码在 ``app/services/errors.py`` 的 ``ErrorCode`` 里统一定义。
"""

from __future__ import annotations


class DecisionReason:
    """决策理由文案。"""

    @staticmethod
    def list_black_hit(dimension_cn: str, count: int) -> str:
        return f"命中黑名单（{dimension_cn}，{count} 条），直接拒绝"

    @staticmethod
    def list_white_hit(dimension_cn: str, count: int) -> str:
        return f"命中白名单（{dimension_cn}，{count} 条），直接放行"

    @staticmethod
    def list_conflict(policy: str) -> str:
        return f"黑白名单同时命中，按冲突策略「{policy}」仲裁"

    @staticmethod
    def score_pass(risk_score: int, review_threshold: int) -> str:
        return f"综合分 {risk_score} 低于审核阈值 {review_threshold}，判定为低风险放行"

    @staticmethod
    def score_review(risk_score: int, review_threshold: int) -> str:
        return f"综合分 {risk_score} 达到审核阈值 {review_threshold}，转人工审核"

    @staticmethod
    def score_challenge(risk_score: int, review_threshold: int) -> str:
        return f"综合分 {risk_score} 达到审核阈值 {review_threshold}，且命中需二次验证的规则"

    @staticmethod
    def score_reject(risk_score: int, reject_threshold: int) -> str:
        return f"综合分 {risk_score} 达到拒绝阈值 {reject_threshold}，判定为高风险并拒绝"


class ValidationMessage:
    """事件接入校验失败文案。"""

    EVENT_ID_DUPLICATED = "事件编号已存在，请使用新的 event_id（幂等键不允许复用）"
    EVENT_TIME_TOO_FAR = "事件时间为未来时间，超出允许的时间偏差，请检查设备时钟"
    ORDER_NOT_FOUND = "订单不存在，无法为该事件计算有效特征"
    ORDER_USER_MISMATCH = "事件所属用户与订单归属用户不一致"
    REFUND_NOT_FOUND = "退款单不存在"
    PAY_TARGET_NOT_PAID_READY = "订单当前状态不允许支付（可能已支付或已取消）"


# 维度中文名（用于决策理由与界面展示）
DIMENSION_CN: dict[str, str] = {
    "user": "用户",
    "phone": "手机号",
    "ip": "IP",
    "device": "设备",
    "address": "收货地址",
}


def dimension_cn(dimension: str) -> str:
    """维度英文名 -> 中文名（未知维度原样返回，避免展示层出现空字符串）。"""
    return DIMENSION_CN.get(dimension, dimension)
