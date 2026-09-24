"""模拟业务端与作弊场景的回归测试。

**为什么场景要进测试套件**：它们是 P0 验收的结论性证据（docs/PRD.md §14.2 / §14.3）。
场景脚本自带 ``expected`` 断言，但"只在演示时人工跑一次"等于没有断言 ——
策略微调（改阈值、改权重、改特征口径）之后场景不再成立时，日志看起来一切正常，
只是分数悄悄落进了另一个动作区间，没有任何人会察觉。

因此这里写入**真实种子规则**（``app.seeds.rules``，不是测试专用规则），
在干净的测试库与 Redis DB=1 上重跑三个场景。种子规则的改动若让场景失真，
这几个测试会立即失败 —— 它们是规则权重与阈值口径的"场景级"回归网。

覆盖的失败模式（都是"静默"的，不会自己报错）：

1. 单号/事件号撞车：事件号重复会让后来者被幂等缓存回放成前一条事件的决策；
2. 场景不再产出预期动作：规则分落进别的区间；
3. 场景产出的动作"对了"但路径错了：由别的规则误打误撞拦下来。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.seeds import rules as rules_seed
from app.services import config_service, list_service, model_engine, rule_engine
from app.simulator.scenarios import coupon_farm, normal_day, refund_fraud
from app.simulator.service import BusinessSimulator

pytestmark = pytest.mark.integration

# 每个测试前后清空的表：决策产物 + 策略 + 业务单据。
# 用 TRUNCATE 而不是 DELETE：rc_audit_log 上有"只增"触发器（BEFORE DELETE 直接抛错），
# 而 TRUNCATE 是 DDL，不触发 DML 触发器。
_TRUNCATE_TABLES = (
    "rc_event",
    "rc_feature_snapshot",
    "rc_decision",
    "rc_decision_hit",
    "rc_model_contribution",
    "rc_audit_log",
    "rc_rule",
    "rc_rule_version",
    "rc_list_entry",
    "rc_model_version",
    "sys_config",
    "sys_api_key",
    "sys_user",
    "biz_order",
    "biz_refund",
    "biz_coupon_receive",
    "biz_customer",
)


def _truncate(db: Session) -> None:
    """清空测试库中的业务表（DDL，因此能绕过审计表的只增触发器）。"""
    db.rollback()
    for name in _TRUNCATE_TABLES:
        db.execute(text(f"TRUNCATE TABLE {name}"))
    db.commit()


@pytest.fixture
def sim_db(engine, redis_client) -> Session:
    """会真正提交的会话 + 种子规则。

    模拟端自己提交事务（它扮演的是"业务系统"，与网关是两次独立事务），
    所以不能用默认的 ``db`` 夹具（回滚语义会让落库结果全部消失）。
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    _truncate(session)
    rules_seed.run(session)
    config_service.invalidate_cache()
    rule_engine.invalidate_cache()
    list_service.invalidate_cache()
    model_engine.reset()
    try:
        yield session
    finally:
        session.rollback()
        # 收尾再清一次：本文件里的场景会真的写入业务单据与决策，
        # 残留数据会污染后续测试（尤其是不做 truncate 的单元测试）。
        _truncate(session)
        session.close()


def test_business_numbers_are_unique_within_process() -> None:
    """单号在"多个实例、同一秒"的情形下也必须唯一。

    这是曾经踩过的坑：序号挂在 ``BusinessSimulator`` 实例上，而每个场景各建一个实例，
    于是同一秒内两个实例都从 1 开始计数，生成出完全相同的订单号与事件号 ——
    前者撞唯一约束抛 IntegrityError，后者更隐蔽：事件号重复会被幂等缓存当作
    "重复投递"，直接回放上一条事件的决策，整条事件静默丢失。

    这里不需要数据库：只验证号生成器本身的不变量。
    """
    numbers = [BusinessSimulator(None)._next_no("EVT") for _ in range(500)]
    assert len(set(numbers)) == 500


def test_coupon_farm_scenario_rejects_device_farm(sim_db: Session) -> None:
    """场景一：设备农场批量领券 → Reject，且靠"环境 + 同设备聚集"识别。"""
    report = coupon_farm(
        sim_db,
        seed=20260923,
        accounts=9,
        devices=3,
        rounds=2,
        stealth_accounts=1,
        prefix_index=900000,
    )

    assert report.check() == []
    assert report.rejected > 0


def test_refund_fraud_scenario_scored_up_to_review(sim_db: Session) -> None:
    """场景二：单笔证据不足 → 连续退款叠加后进入人工审核区间。

    断言的是"逐笔升级"这个设计：前几笔退款分数低于人审阈值（单笔不足以定级），
    最后一笔跨过 60 落进 60~79。只断言"出现了 Review"是不够的 ——
    那样一条把阈值整体调低的改动也能让测试通过，而 PRD §14.3 要的正是这个分寸。
    """
    report = refund_fraud(
        sim_db, seed=20260923, prior_accounts=4, refund_count=3, prefix_index=800000
    )

    assert report.check() == []

    main_refunds = [
        step
        for step in report.steps
        if step.decision.get("user_id") == "U800999"
        and step.decision.get("event_type") == "after_sale_apply"
    ]
    assert len(main_refunds) == 3
    scores = [int(step.decision["risk_score"]) for step in main_refunds]
    assert scores[:2] == sorted(scores[:2])
    assert all(score < 60 for score in scores[:2]), "前两笔应当仍在放行区间"
    assert 60 <= scores[-1] < 80, "最后一笔应当落进人工审核区间"
    assert main_refunds[-1].action == "Review"


def test_normal_day_has_no_false_reject(sim_db: Session) -> None:
    """场景三：正常流量不得出现 Reject（误拦基线）。"""
    report = normal_day(sim_db, seed=20260923, users=12, prefix_index=100000)

    assert report.check() == []
    assert report.rejected == 0
