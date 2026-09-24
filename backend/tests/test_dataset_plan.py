"""数据集生成器的**规划层**回归测试（docs/PRD.md §14.1）。

**为什么只测规划层**：5 万事件落库约 20 分钟，而"五类事件占比、作弊事件占比、
账号归属、时间线边界"全部由 ``plan_actions`` 这一纯计算阶段决定（见其 docstring），
落库只是把计划执行一遍。因此这里对规划结果做断言：秒级完成、不需要 MySQL 与 Redis，
却能把"生成器悄悄跑偏"这类**不报错**的问题拦在提交之前。

覆盖的失败模式（都是静默的）：

1. 时间线越界 —— 业务事件跑到窗口之外（``night_activity_ratio`` 等按小时切分的
   特征会跟着漂移）；注册事件早于窗口是**合法**的（注册时刻 = 账号年龄），
   因此两者在下面分开断言；
2. 可复现性失效 —— 同 seed + 同 anchor 两次规划不一致（例如实现里偷偷用
   ``utcnow()``），数据集就再也无法复跑；
3. 标签归属错位 —— 有账号拿不到任何事件（某类画像静默消失），或作弊账号数不符；
4. 分布漂移 —— 改会话模板后某类事件占比超出容忍区间（PRD §14.1）。

这些断言是 PRD 口径的**代码化**：文档里的数字没有断言守着，改一次就失真。

**用例规模分两档**：PRD 默认口径（5 万事件 / 800 账号）用于分布类断言 ——
占比区间与容差本来就是为这个量级标定的，缩小规模会让"每人配额取整"的误差
被放大到失真；小规模 spec 只用于可复现性、时间线、排序这类与规模无关的不变量。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from app.simulator.dataset import (
    EXPECTED_CHEAT_RATIO,
    EXPECTED_TYPE_MIX,
    TYPE_MIX_TOLERANCE,
    DatasetSpec,
    _CHEAT_EVENTS_PER_ACCOUNT,
    plan_actions,
)

# 固定时间锚点：不写死的话，"同 seed 同 anchor 必得同一结果"这条断言自身
# 就会随运行时刻漂移（而它正是本文件要守的不变量）。
ANCHOR = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)

# 作弊画像在**规划内部**使用的主体画像名：``build_actor`` 会把 stealth 那类
# 改写成 ``stealth_farm``（见 actors.build_cheat_actor）。这里按实际取值断言，
# 避免用 ``_CHEAT_EVENTS_PER_ACCOUNT`` 的键名去数动作而数出 0（口径错位的陷阱）。
CHEAT_PROFILES = ("device_farm", "stealth_farm", "refund_fraud")

# 小规模 spec：只用于与规模无关的不变量（可复现性 / 时间线 / 排序 / 健壮性）。
SMOKE = DatasetSpec(events=4_000, days=7, accounts=200, cheat_accounts=20, seed=7, anchor=ANCHOR)


@pytest.fixture(scope="module")
def prd_plan():
    """PRD §14.1 默认口径的规划结果（模块级共享：一次规划约 5 秒）。"""
    return plan_actions(DatasetSpec(), batch=900_000)


def _plan(spec: DatasetSpec = SMOKE):
    """小规模规划（固定 batch，保证与账号号段相关的取值也可复现）。"""
    return plan_actions(spec, batch=900_001)


def _fingerprint(plan):
    """动作序列的完整指纹：时间、类型、账号、金额都要一致才算可复现。"""
    return [
        (action.kind, action.occurred_at, action.actor.user_id, round(action.amount, 6))
        for action in plan.actions
    ]


def test_plan_is_reproducible_with_same_seed_and_anchor():
    """同 seed + 同 anchor 两次规划必须完全一致（PRD §14.1 要求结果可复现）。"""
    first = _plan()
    second = _plan()

    assert _fingerprint(first) == _fingerprint(second)
    assert (first.start, first.end) == (second.start, second.end)
    assert first.cheat_events == second.cheat_events


def test_plan_span_follows_anchor_and_days():
    """时间线右端点 = anchor，跨度 = days（可复现性的前提）。"""
    plan = _plan()

    assert plan.end == ANCHOR
    assert plan.end - plan.start == timedelta(days=SMOKE.days)


@pytest.mark.parametrize("days", [1, 3, 14])
def test_span_is_exact_for_any_window(days):
    """任意跨度下窗口都严格等于 ``days``（防"少一天/多一天"这类边界错误）。"""
    spec = DatasetSpec(events=400, days=days, accounts=20, cheat_accounts=2, seed=3, anchor=ANCHOR)
    plan = plan_actions(spec, batch=900_003)
    assert plan.end - plan.start == timedelta(days=days)


def test_business_actions_stay_inside_the_window(prd_plan):
    """**业务动作**不得越过窗口边界；注册动作例外（注册时刻 = 账号年龄）。

    注册事件早于窗口起点是设计要求：账号年龄要能覆盖 1~900 天，
    否则``account_age_days`` 会退化成"都是新账号"。但业务动作越界就是缺陷 ——
    它会让"某天"的统计口径整体偏移。
    """
    business = [a for a in prd_plan.actions if a.kind != "register"]
    too_late = [a for a in business if a.occurred_at > prd_plan.end]
    too_early = [a for a in business if a.occurred_at < prd_plan.start]

    assert too_late == []
    # 允许极少量"会话跨过左边界"的溢出：实测 2 条（约 0.004%），
    # 但整体漂移（例如基准时刻取错）会让这个数量级跳变。
    assert len(too_early) <= len(business) * 0.001


def test_actions_are_sorted_by_time():
    """动作必须按时间升序：乱序执行会让滑动窗口特征看到"未来"的事件。"""
    plan = _plan()
    moments = [action.occurred_at for action in plan.actions]
    assert moments == sorted(moments)


def test_event_total_matches_prd_quota(prd_plan):
    """事件总量贴住 5 万配额（容差 5%，实测 +2.2%）。

    超出部分来自两处**取整**：正常账号的每人预算按 ``round(总配额/账号数)`` 分摊，
    且作弊画像的事件预算是固定值（不随配额缩放）。因此这里断的是"同量级"，
    而不是逐条相等 —— 逐条相等需要按配额反解每人预算，会让画像失真。
    """
    total = sum(prd_plan.type_mix().values())
    assert total == pytest.approx(DatasetSpec().events, rel=0.05)


def test_event_type_mix_matches_prd_14_1(prd_plan):
    """五类事件占比必须落在 PRD §14.1 的目标 ±4 个百分点内。"""
    mix = prd_plan.type_mix()
    total = sum(mix.values())

    assert set(EXPECTED_TYPE_MIX) <= set(mix), "规划里缺少某一类事件（比例为 0 不会报错）"
    for event_type, expected in EXPECTED_TYPE_MIX.items():
        actual = mix[event_type] / total
        assert abs(actual - expected) <= TYPE_MIX_TOLERANCE, (
            f"{event_type} 占比 {actual:.4f} 偏离目标 {expected} "
            f"超过容忍 {TYPE_MIX_TOLERANCE}"
        )


def test_cheat_ratio_recomputed_from_actions(prd_plan):
    """作弊事件占比落进 6%~8%，且**由账号预算涌现**（不是循环内调参凑的）。

    两种口径必须互相印证：规划自报的 ``cheat_events``，与"按画像直接数动作"的结果。
    只信一个变量时，"同一个变量算错两次"这类问题看不出来。
    """
    counted = Counter(
        action.actor.profile for action in prd_plan.actions if action.kind != "register"
    )
    by_profile = sum(counted[name] for name in CHEAT_PROFILES)
    assert by_profile == prd_plan.cheat_events

    low, high = EXPECTED_CHEAT_RATIO
    ratio = prd_plan.cheat_events / sum(counted.values())
    assert low <= ratio <= high, f"作弊事件占比 {ratio:.4f} 超出 {low}~{high}"


def test_accounts_are_labelled_by_persona(prd_plan):
    """账号数、作弊账号数、账号归属三者一致（标签错位会直接毁掉训练集）。"""
    cheat_users = {
        action.actor.user_id
        for action in prd_plan.actions
        if action.actor.profile in CHEAT_PROFILES
    }
    normal_users = {
        action.actor.user_id
        for action in prd_plan.actions
        if action.actor.profile not in CHEAT_PROFILES
    }

    spec = DatasetSpec()
    assert prd_plan.accounts == spec.accounts
    assert prd_plan.cheat_accounts == spec.cheat_accounts == len(cheat_users)
    assert len(normal_users) == spec.accounts - spec.cheat_accounts
    assert not (cheat_users & normal_users), "同一个账号不能既是作弊又是正常"


def test_each_persona_gets_events_within_its_budget(prd_plan):
    """每类画像都必须产出事件，且单账号事件数落在预算量级内。

    下界更重要：某类画像若拿到 0 条事件，训练集里"少了一类样本"不会有任何报错，
    只会让模型在那一类上完全没有判别力。
    """
    per_account: dict[str, list[int]] = defaultdict(list)
    counts: Counter = Counter()
    for action in prd_plan.actions:
        if action.kind != "register":
            counts[(action.actor.profile, action.actor.user_id)] += 1
    for (profile, _user_id), count in counts.items():
        per_account[profile].append(count)

    # 三类作弊画像：预算基数 ±20%（掩护会话的数量本身由模板抽样决定）
    for profile, budget_key in (
        ("device_farm", "device_farm"),
        ("stealth_farm", "stealth"),
        ("refund_fraud", "refund_fraud"),
    ):
        budget = _CHEAT_EVENTS_PER_ACCOUNT[budget_key]
        values = per_account[profile]
        assert values, f"画像 {profile} 没有任何事件"
        assert min(values) >= budget * 0.8
        assert max(values) <= budget * 1.2

    # 三类正常子画像（含领券党、企业出口、合租共用设备）
    assert per_account["normal"]
    assert per_account["normal_vpn"]
    assert per_account["shared_home"]


def test_more_cheat_accounts_than_accounts_does_not_crash():
    """作弊账号数超过总账号数时退化为"没有正常账号"，而不是抛异常或负配额。"""
    spec = DatasetSpec(events=100, days=1, accounts=5, cheat_accounts=8, seed=1, anchor=ANCHOR)
    plan = plan_actions(spec, batch=900_002)

    assert plan.normal_accounts == 0
    assert all(action.actor.profile in CHEAT_PROFILES for action in plan.actions)