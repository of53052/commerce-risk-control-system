"""rc_rule 种子：20 条预置规则（P0，docs/PRD.md 17.1）。

**关键设计：种子只写表达式文本，AST 由 parse() 现算。**

为什么不是"手写一份 JSON AST + 一份文本"：
    两份手写数据必然漂移，而漂移的表现是"界面上看到的条件"与"实际执行的逻辑"
    不一致 —— 这是风控系统里最危险的一类不一致（用户以为拦的是 A，实际拦的是 B）。
    这里让 ``condition_text`` 成为人写的唯一来源，AST 由同一个解析器产出、
    并逐条过 ``validate_node`` 的字段白名单校验；
    因此"文本与 AST 不等价"这件事在结构上不可能发生。

规则的场景（scene）分配：
    - ``all``        ：通用风控规则（设备/IP/环境/名单类），对所有事件类型生效
    - ``coupon``     ：领券场景
    - ``order``      ：下单场景
    - ``after_sale`` ：售后场景

``action_hint=challenge`` 的规则命中且综合分进入中风险区间时，
动作会从 Review 变为 Challenge（二次验证，不建案）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.expression import node_to_dict, parse, render, validate_node
from app.models.rule import (
    ACTION_HINT_CHALLENGE,
    ACTION_HINT_NONE,
    CATEGORY_AFTERSALE,
    CATEGORY_AMOUNT,
    CATEGORY_ENVIRONMENT,
    CATEGORY_FREQUENCY,
    CATEGORY_LIST,
    RcRule,
    RcRuleVersion,
)
from app.services.feature_engine import known_feature_keys
from app.services.rule_engine import invalidate_cache

NAME = "rules"
logger = logging.getLogger("app.seeds.rules")


@dataclass(frozen=True)
class RuleSeed:
    """一条规则的声明（condition_text 是唯一真源）。"""

    code: str
    name: str
    scene: str
    category: str
    text: str
    score: int
    priority: int = 100
    action_hint: str = ACTION_HINT_NONE
    description: str = ""


# --------------------------------------------------------------------------- #
# 20 条预置规则
# --------------------------------------------------------------------------- #
RULE_SEEDS: tuple[RuleSeed, ...] = (
    # ---- 通用：频次聚集（all，对所有场景生效）----
    RuleSeed(
        code="RC_FREQ_001",
        name="同设备关联多账号",
        scene="all",
        category=CATEGORY_FREQUENCY,
        text="device_account_cnt_24h >= 3",
        score=25,
        priority=10,
        description="24 小时内同一设备上出现 3 个及以上账号，典型的多开/养号特征",
    ),
    RuleSeed(
        code="RC_FREQ_002",
        name="单用户短时高频领券",
        scene="coupon",
        category=CATEGORY_FREQUENCY,
        text="user_coupon_cnt_1h >= 5",
        score=20,
        priority=20,
        action_hint=ACTION_HINT_CHALLENGE,
        description="1 小时内领券 5 次以上，先二次验证而不是直接拦截（可能是正常凑单）",
    ),
    RuleSeed(
        code="RC_FREQ_003",
        name="同 IP 多账号集中领券",
        scene="coupon",
        category=CATEGORY_FREQUENCY,
        text="ip_account_cnt_24h >= 3 and ip_coupon_cnt_1h >= 5",
        score=25,
        priority=15,
        description="同一出口 IP 下多账号集中领券，典型的羊毛党工作室特征",
    ),
    RuleSeed(
        code="RC_FREQ_004",
        name="单用户短时高频下单",
        scene="order",
        category=CATEGORY_FREQUENCY,
        text="user_order_cnt_1h >= 5",
        score=20,
        priority=20,
        description="1 小时内下单 5 单以上，可能是刷单或盗号盗刷",
    ),

    # ---- 通用：环境指纹（all）----
    RuleSeed(
        code="RC_ENV_001",
        name="高风险设备环境",
        scene="all",
        category=CATEGORY_ENVIRONMENT,
        text="device_env_risk >= 60",
        score=30,
        priority=5,
        description="指纹命中模拟器/多开/虚拟定位等多项可疑信号",
    ),
    RuleSeed(
        code="RC_ENV_002",
        name="数据中心或代理 IP",
        scene="all",
        category=CATEGORY_ENVIRONMENT,
        text="ip_is_datacenter == true",
        score=20,
        priority=30,
        description="IP 属于机房/代理段，正常用户极少从此类网段访问",
    ),
    RuleSeed(
        code="RC_ENV_003",
        name="IP 归属地与收货地不一致",
        scene="all",
        category=CATEGORY_ENVIRONMENT,
        text="ip_region_mismatch == true",
        score=15,
        priority=40,
        description="下单 IP 与收货地区域不符，常见于代下单与团伙作案；单独不足以定级，作为加成",
    ),
    RuleSeed(
        code="RC_ENV_004",
        name="新账号叠加高风险环境",
        scene="all",
        category=CATEGORY_ENVIRONMENT,
        text="account_age_days < 3 and device_env_risk >= 40",
        score=20,
        priority=12,
        action_hint=ACTION_HINT_CHALLENGE,
        description="注册不足 3 天且环境可疑，先二次验证（短信/人脸）成本低于误拦",
    ),

    # ---- 金额类 ----
    RuleSeed(
        code="RC_AMT_001",
        name="单用户大额领券",
        scene="coupon",
        category=CATEGORY_AMOUNT,
        text="user_coupon_amount_24h >= 200",
        score=20,
        priority=50,
        description="24 小时领券面额合计超过 200 元，超出正常消费节奏",
    ),
    RuleSeed(
        code="RC_AMT_002",
        name="高额订单",
        scene="order",
        category=CATEGORY_AMOUNT,
        text="user_order_amount_24h >= 5000",
        score=20,
        priority=50,
        description="24 小时下单金额合计超 5000 元，关注是否为盗刷",
    ),
    RuleSeed(
        code="RC_AMT_003",
        name="大额退款申请",
        scene="after_sale",
        category=CATEGORY_AMOUNT,
        text="user_refund_amount_24h >= 2000",
        score=30,
        priority=20,
        description="24 小时退款金额合计超 2000 元，恶意退款欺诈的主要信号",
    ),
    RuleSeed(
        code="RC_AMT_004",
        name="新账号高额订单",
        scene="order",
        category=CATEGORY_AMOUNT,
        text="account_age_days < 7 and user_order_amount_24h >= 3000",
        score=30,
        priority=15,
        description="注册不足 7 天即下大额订单，盗号盗刷的高发组合",
    ),

    # ---- 售后欺诈 ----
    RuleSeed(
        code="RC_AS_001",
        name="7 天内高频退款",
        scene="after_sale",
        category=CATEGORY_AFTERSALE,
        text="user_refund_cnt_7d >= 8",
        score=30,
        priority=10,
        description="7 天退款申请 8 次以上，明显超出正常售后频率",
    ),
    RuleSeed(
        code="RC_AS_002",
        name="退款率异常偏高",
        scene="after_sale",
        category=CATEGORY_AFTERSALE,
        text="user_refund_rate_24h >= 0.5 and user_order_cnt_24h >= 4",
        score=30,
        priority=12,
        description="24 小时内退款率过半且订单数不低于 4，排除「只有一单」的噪声",
    ),
    RuleSeed(
        code="RC_AS_003",
        name="首单即退款",
        scene="after_sale",
        category=CATEGORY_AFTERSALE,
        text="user_first_order_refund == true",
        score=25,
        priority=15,
        description="第一单就申请退款，是薅运费险/恶意退款的典型起点",
    ),
    RuleSeed(
        code="RC_AS_004",
        name="同收货地址退款聚集",
        scene="after_sale",
        category=CATEGORY_AFTERSALE,
        text="address_refund_cnt_7d >= 3",
        score=25,
        priority=18,
        description="同一收货地址 7 天内多次退款，指向同一收货点的团伙欺诈",
    ),

    # ---- 名单与聚集 ----
    RuleSeed(
        code="RC_LIST_001",
        name="用户灰名单加成",
        scene="all",
        category=CATEGORY_LIST,
        text="subject_gray_flag == 1",
        score=20,
        priority=60,
        description="灰名单不直接定动作，而是提高综合分让规则与模型更容易命中",
    ),
    RuleSeed(
        code="RC_LIST_002",
        name="设备灰名单加成",
        scene="all",
        category=CATEGORY_LIST,
        text="device_gray_flag == 1",
        score=15,
        priority=62,
        description="可疑设备（非直接封禁）作为加成信号",
    ),
    RuleSeed(
        code="RC_LIST_003",
        name="同收货地址多账号",
        scene="order",
        category=CATEGORY_LIST,
        text="address_account_cnt_7d >= 3",
        score=25,
        priority=25,
        description="7 天内同一收货地址关联 3 个及以上账号，指向代收点/团伙",
    ),
    RuleSeed(
        code="RC_LIST_004",
        name="同设备新账号占比过高",
        scene="order",
        category=CATEGORY_LIST,
        text="device_new_account_ratio_24h >= 0.8 and device_account_cnt_24h >= 3",
        score=30,
        priority=22,
        description="同一设备上的账号几乎全是新账号，典型的批量注册养号",
    ),
)


def _build_condition(seed: RuleSeed, known_fields: set[str]) -> tuple[dict, str]:
    """把表达式文本解析成 AST 并做白名单校验。

    校验失败直接抛错，让种子写入整体失败 ——
    一条引用不存在字段的规则入库后会"永远不命中"，
    这种静默失效比种子报错危险得多。
    """
    ast = parse(seed.text)
    problems = validate_node(ast, known_fields)
    if problems:
        raise ValueError(f"规则 {seed.code} 条件不合法：{problems}")
    return node_to_dict(ast), render(ast)


def run(db: Session) -> int:
    """写入 20 条规则（幂等：按 code upsert，内容变化时版本 +1 并留快照）。"""
    known_fields = known_feature_keys(db)
    existing = {row.code: row for row in db.execute(select(RcRule)).scalars()}
    affected = 0

    for seed in RULE_SEEDS:
        condition, condition_text = _build_condition(seed, known_fields)
        row = existing.get(seed.code)

        if row is None:
            row = RcRule(
                code=seed.code,
                name=seed.name,
                scene=seed.scene,
                category=seed.category,
                condition=condition,
                condition_text=condition_text,
                score=seed.score,
                action_hint=seed.action_hint,
                priority=seed.priority,
                enabled=True,
                version=1,
                description=seed.description,
                created_by="seed",
                updated_by="seed",
            )
            db.add(row)
            db.flush()
            # 首次写入也要留版本快照：rc_rule_version 是"只增"的审计证据，
            # 缺了 v1 会让后续的变更历史出现无法解释的断点。
            _snapshot(db, row, change_type="create")
            affected += 1
            continue

        changed = (
            row.condition != condition
            or row.score != seed.score
            or row.action_hint != seed.action_hint
            or row.scene != seed.scene
            or row.priority != seed.priority
        )
        if not changed:
            continue

        row.name = seed.name
        row.scene = seed.scene
        row.category = seed.category
        row.condition = condition
        row.condition_text = condition_text
        row.score = seed.score
        row.action_hint = seed.action_hint
        row.priority = seed.priority
        row.description = seed.description
        row.version += 1
        row.updated_by = "seed"
        _snapshot(db, row, change_type="update")
        affected += 1

    # 规则内容变了，进程内编译缓存必须失效，否则新规则要等重启才生效
    invalidate_cache()
    if affected:
        logger.info("规则种子写入完成，变更 %d 条", affected)
    return affected


def _snapshot(db: Session, row: RcRule, *, change_type: str) -> None:
    """写入规则版本快照（只增）。"""
    db.add(
        RcRuleVersion(
            rule_code=row.code,
            version=row.version,
            condition=row.condition,
            condition_text=row.condition_text,
            score=row.score,
            action_hint=row.action_hint,
            enabled=row.enabled,
            change_type=change_type,
            changed_by="seed",
        )
    )
