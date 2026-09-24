"""数据集生成器：14 天混合流量 + 场景标签（docs/PRD.md §14.1）。

目标规模（PRD §14.1）：

====== ==============================================
项     取值
====== ==============================================
事件量 50,000 条
跨度   14 天
账号   约 800 个（含 60~80 个作弊账号）
作弊比 6% ~ 8%
事件比 login 35% / order_create 20% / coupon_receive 18%
       / order_pay 15% / after_sale_apply 12%
====== ==============================================

**为什么必须走真实链路（事件网关）而不是直接写表**：训练特征与线上特征必须同源。
自己另写一套聚合逻辑来"快速造特征"，会得到一份离线 AUC 很好看、线上权重全偏的模型，
而这类问题在演示里几乎不可能被发现（页面照样显示分数，只是分数没有意义）。
代价是每条事件约 20ms（特征计算 + 5 张表落库），5 万条约 17 分钟 —— 一次性成本。

**为什么作弊样本要压到 6%~8%**：真实风控里欺诈是少数类。按 50/50 生成，
模型会学到"随便猜都能对一半以上"，AUC 虚高却毫无判别力；正样本再少又会
让逻辑回归权重不稳。6%~8% 是"够学、又不失真"的区间，也是
``class_weight=balanced`` 能真正起作用的前提。

**事件类型分布为什么是"不真实"的**：PRD §14.1 规定退款占 12%，而真实电商的
自然退款率只有 1%~3%。这里按 PRD 的口径生成，换来的是"售后欺诈"这一类
有足够样本可学（否则两张退款规则的权重会被噪声主导）。代价已在 PRD §15 的
风险表登记：合成数据集训练出的指标仅供演示，不代表真实业务表现。

**标签口径**：``rc_event.is_cheat``：1 = 作弊账号产生的事件，0 = 正常账号产生，
NULL = 线上真实事件。生成器只写 0/1，训练脚本显式过滤 NULL，
因此数据集库与线上库可以共用同一套表结构而不会互相污染。
"""

from __future__ import annotations

import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.event import RcEvent
from app.simulator.actors import (
    NORMAL_FINGERPRINTS,
    Actor,
    Device,
    build_actor,
    build_device_pool,
    build_shared_home_actor,
)
from app.simulator.service import BusinessResult, BusinessSimulator

logger = logging.getLogger("app.simulator.dataset")

# 事件类型占比（PRD §14.1）
EXPECTED_TYPE_MIX: dict[str, float] = {
    "login": 0.35,
    "order_create": 0.20,
    "coupon_receive": 0.18,
    "order_pay": 0.15,
    "after_sale_apply": 0.12,
}

# 作弊样本占比区间（PRD §14.1）
EXPECTED_CHEAT_RATIO = (0.06, 0.08)

# 事件类型占比的容忍偏差（百分点）：生成器用"会话模板"近似分布，
# 不可能分毫不差。超过 4 个百分点说明模板被动过，需要重新标定。
TYPE_MIX_TOLERANCE = 0.04


# --------------------------------------------------------------------------- #
# 会话模板：正常用户的一次浏览行为
# --------------------------------------------------------------------------- #
# 概率是按 PRD §14.1 的目标分布**反解**出来的（每会话期望 2.78 条事件）：
#   登录 1 → 35%，领券 0.44 → 18%，下单 0.57 → 20%，
#   支付 0.57×0.75 = 0.43 → 15%，退款 0.43×0.8 = 0.34 → 12%。
#
# **这里有一个必须说清楚的数学约束**：PRD §14.1 同时规定
# "支付占 15%、售后占 12%"，即**售后 / 支付 = 0.8**；
# 而"售后 / 下单 = 0.6"，也就是说 PRD 的分布**强制要求 60% 的订单发生退款**。
# 真实电商的自然退款率是 1%~3%，所以这不是业务直觉，是一个口径选择。
# 由此带来两个后果，都已登记为已知限制（PRD §15）：
#
#   * `user_refund_rate_24h` / `address_refund_rate_7d` 这类特征在正常账号上
#     也会达到 0.6 上下，与"恶意退款"画像的 1.0 区间部分重叠，
#     判别力被压缩（模型只能靠金额、频次、首单即退款等旁证来区分）；
#   * 想要"退款率特征有清晰方向"（正常 0.25 vs 作弊 1.0），
#     就必须把售后占比降到 5% 上下 —— 那需要改 PRD §14.1，属于规格变更。
#
# 代码这里**按 PRD 口径执行**，并把偏差控制在容忍区间内；
# 如果将来调整口径，只需改这一组数字 + `EXPECTED_TYPE_MIX`，
# 生成器其余部分不需要动（`problems()` 会在偏离超过 4% 时报警）。
#
# 实测标定：作弊账号的领券占比天然偏高（农场型账号的行为就是刷券），
# 领券党（12% 的正常账号，一次蹲券连领 3~7 张）又会再推高一截，
# 因此正常会话里的领券概率从最初的 0.51 下调到 0.35，
# 混合后的总占比才落到 18% 附近。
# 这个数不能凭公式算出来 —— 它取决于作弊画像配比与两个子画像的占比，
# 改动任何一项都要重新标定（用 scripts 里的分布标定脚本，秒级出结果；
# `problems()` 会在偏离超过 4 个百分点时报警）。
_SESSION_TEMPLATE: tuple[tuple[str, float], ...] = (
    ("login", 1.0),
    ("coupon_receive", 0.35),
    ("order_create", 0.57),
    ("order_pay", 0.75),
    ("after_sale_apply", 0.80),
)

# 会话模板按名字取概率：动作生成与"目标分布"是同一组数字，
# 否则标定模板时容易只改一处，另一处继续用旧值（那种偏差很难察觉）。
_SESSION_PROB: dict[str, float] = dict(_SESSION_TEMPLATE)



# 作弊账号的三类画像占比（先按比例分账，再按顺序取整，保证总数精确）
#
# 隐蔽型从 20% 提到 35%：**这是为了让数据集具备判别难度，而不是为了让指标好看**。
# 如果作弊账号清一色是"农场设备 + 代理 IP + 共用地址"，模型只要看环境特征
# 就能 100% 分开两类样本，AUC 必然等于 1.0 —— 那个数字说明的是"我把两类账号
# 画得完全不一样"，而不是"模型有判别力"。真实黑产里相当一部分账号用的就是
# 真机、家宽、独立地址，只能靠行为序列识别，这部分样本占比太低，
# 训练出来的模型在线上遇到"干净账号"时会直接失效。
_CHEAT_PROFILE_SHARE: tuple[tuple[str, float], ...] = (
    ("device_farm", 0.35),
    ("stealth", 0.35),
    ("refund_fraud", 0.30),
)

# 正常账号里的"可疑环境"占比：合租房共用地址、公司/学校共用出口 IP、
# 家人共用一台设备。这些用户完全正常，但环境特征与作弊画像重叠。
# **没有这部分样本，模型学到的就是"环境可疑 = 作弊"** ——
# 一条线上会造成大规模误拦的规律（真实风控里最贵的一类错误）。
_NORMAL_SUSPICIOUS_SHARE = 0.18

# 作弊账号的"掩护行为"占比：一个作弊账号的全部事件里，有多大比例是
# **和普通用户一模一样的浏览/下单**。
#
# **这是整个生成器里最关键的一条保真规则**。如果作弊账号的事件全是作弊动作
# （农场型只有"登录 + 领券"、退款型只有"下单 + 支付 + 退款"），那么它的每一个
# 事件在特征空间里都带着极端值，模型只要看单个特征的高分位就能全部分开 ——
# 实测 AUC 冲到 0.9994，而真实风控模型的 AUC 通常在 0.8~0.95：
# 差额正是来自"黑产账号也会正常消费"这部分掩护行为。
#
# 掩护行为还带来一个必须承认的副作用：**标签噪声**。同一个作弊账号的正常浏览
# 事件在账号级标签下仍被标成作弊（口径是"作弊账号产生的事件"），
# 这在真实系统里本来就是如此 —— 人工确认封禁一个账号时，
# 它过去一个月的正常消费记录也一并被算进"黑样本"。
# 这个噪声是数据集该有的，不是缺陷。
_CHEAT_COVER_SHARE: dict[str, float] = {
    "device_farm": 0.50,
    "stealth": 0.50,
    "refund_fraud": 0.30,
}

# 正常账号的注册年龄分布（天，按权重）：**必须是分布，而不是一个区间**。
#
# 真实平台的账号池里有相当比例是"最近才注册的新用户"（这里给了 22% 在 30 天内、
# 28% 在半年内），而不是清一色几百天的老账号。如果正常账号全是老账号，
# ``account_age_days`` 就变成了"新账号即作弊"的一刀切特征 ——
# 这条规律在演示数据上永远成立（作弊画像确实是新账号），上线后却会把
# 每一个真实新用户都拦下。让新用户占一定比例，模型就必须结合行为一起看。
_REGISTER_AGE_BUCKETS: tuple[tuple[tuple[int, int], float], ...] = (
    ((1, 30), 0.22),
    ((30, 180), 0.28),
    ((180, 900), 0.50),
)

# 共用设备/地址的正常用户里，"刚注册的新成员"（家人刚换手机）：
# 每 4 人一个，让"共用设备"与"新账号"两个信号可以独立出现，
# 而不是绑死成"新账号必然共用设备"。
_NEW_MEMBER_EVERY = 4
_NEW_MEMBER_AGE_DAYS: tuple[int, int] = (1, 6)

# 正常订单金额分布（元，按权重）：**长尾**，不是窄区间。
# 真实电商客单价集中在几十到几百元，但存在一条一直延伸到几千元的长尾
# （数码、家电、囤货）。若正常用户只买 50~600 元、作弊账号只买 3000~8000 元，
# 金额就成了又一个"一刀切"特征；给了长尾之后，"高额 + 新账号 + 退款" 才是信号，
# 而单看"买了一台手机"显然不能怀疑用户。
_ORDER_AMOUNT_BUCKETS: tuple[tuple[tuple[float, float], float], ...] = (
    ((30.0, 200.0), 0.45),
    ((200.0, 800.0), 0.33),
    ((800.0, 2000.0), 0.14),
    ((2000.0, 9000.0), 0.08),
)

# 恶意退款账号的订单金额：专挑高价值商品下手（业务动机：退款收益大）。
# 与上面 8% 的正常长尾**大幅重叠**（正常用户也会买手机、家电、囤货），
# 因此金额本身不构成标签；能构成信号的是"高额 + 首单即退 + 同地址多账号"的组合。
_FRAUD_ORDER_AMOUNT: tuple[int, int, int] = (1200, 8000, 100)

_T = TypeVar("_T")


def _weighted_pick(rng: random.Random, table: tuple[tuple[_T, float], ...]) -> _T:
    """按权重从 ``table`` 抽一个取值（``table[i] = (取值, 权重)``）。

    三处分布（订单金额、券面额、注册年龄）本来是同一段循环的三份拷贝，
    抽成共用实现是为了避免"只改了其中一处"的静默偏差：拷贝改漏一处，
    分布会悄悄变样且没有任何报错。权重累加成 [0, Σw) 区间后让随机数落进去。

    末尾兜底返回最后一项：浮点累加的舍入误差可能让 ``point`` 恰等于 Σw
    （循环走完仍未命中），兜底避免返回 None 引发下游 TypeError。
    """
    point = rng.random() * sum(weight for _, weight in table)
    for value, weight in table:
        point -= weight
        if point <= 0:
            return value
    return table[-1][0]


def _order_amount(rng: random.Random) -> float:
    """按 ``_ORDER_AMOUNT_BUCKETS`` 抽一个订单金额（保留 2 位小数）。"""
    span = _weighted_pick(rng, _ORDER_AMOUNT_BUCKETS)
    return round(rng.uniform(*span), 2)

# 正常账号里"环境半可疑"的占比：Root 玩机、云手机、多开、改定位插件的合法用户。
# 没有这部分样本时，``device_env_risk`` 就成了"非 0 即作弊"的一刀切特征
# （见 actors.RISKY_NORMAL_FINGERPRINTS 的说明）。
_NORMAL_RISKY_ENV_SHARE = 0.06

# 正常账号里走 VPN / 企业出口的占比：``is_proxy`` 为真、IP 归属地与收货地不符。
# 没有这部分样本时，``ip_is_datacenter`` 与 ``ip_region_mismatch`` 都是
# "作弊恒为真、正常恒为假"的一刀切特征（见 actors.LEGIT_DATACENTER_IPS）。
_NORMAL_VPN_SHARE = 0.04

# 正常账号里"领券党"的占比：**爱薅券、券领得比谁都勤、但老老实实下单**的用户。
# 他们是薅羊毛账号的真假难辨之处：频次特征上二者高度重叠，
# 区别只在"领完券有没有正常消费"这个行为组合上。
# 没有这部分样本时，`user_coupon_cnt_1h` 会成为又一个一刀切特征
# （作弊 6、正常 ≤1），模型学到的"券领得勤 ⇒ 作弊"上线即误拦领券党。
_NORMAL_COUPON_HUNTER_SHARE = 0.12

# 券面额分布：**正常用户与作弊账号共用同一套面额**。
# 若作弊账号专拿 150 元券、正常用户只拿 10/20/50，`user_coupon_amount_24h`
# 就成了"面额即标签"的泄漏特征。现实里大额券正是领券党与黑产共同的目标，
# 区分二者的从来不是面额，而是**量、频率与后续是否消费**。
_COUPON_VALUES: tuple[tuple[float, float], ...] = (
    (10.0, 0.35),
    (20.0, 0.30),
    (50.0, 0.25),
    (150.0, 0.10),
)
# 领券党与作弊账号更偏好大额券（这是他们共同的行为动机），
# 但**两边用同一张表**，所以金额本身不构成判别依据。
_BIG_COUPON_VALUES: tuple[tuple[float, float], ...] = (
    (50.0, 0.30),
    (150.0, 0.70),
)

# 领券党一次"蹲券"会话的领券张数（与隐蔽型作弊账号的 6 张重叠）
_HUNTER_COUPONS_PER_SESSION = (3, 7)

# 领券党每次登录都顺手去券中心看一眼的概率（普通用户只有 0.35）。
# 领券党的日累计领券量因此与"隐蔽型薅羊毛账号"落在同一量级 ——
# 二者的区别不在"领得勤"，而在**领完之后有没有正常消费**。
_HUNTER_COUPON_SESSION_PROB = 0.8

# 正常账号的两种合法子画像
_PERSONA_NORMAL = "normal"
_PERSONA_HUNTER = "hunter"


def _coupon_value(rng: random.Random, table: tuple[tuple[float, float], ...]) -> float:
    """按权重抽一个券面额（面额表的用意见 ``_COUPON_VALUES`` 的说明）。

    面额是**按权重抽**而不是取平均：``user_coupon_amount_24h`` 是求和特征，
    若所有人都拿同一个面额，这个特征就退化成"领券张数 × 常数"，
    与张数特征完全共线，等于白占一个特征位。
    """
    return _weighted_pick(rng, table)


def _random_register_days(rng: random.Random) -> int:
    """按 ``_REGISTER_AGE_BUCKETS`` 抽一个注册年龄（天）。"""
    low, high = _weighted_pick(rng, _REGISTER_AGE_BUCKETS)
    return rng.randint(low, high)

# 单个作弊账号的计划事件数：三类画像都要落在这个量级，
# 才能让"作弊事件占比"由账号数而不是由个别账号的极端行为决定。
_CHEAT_EVENTS_PER_ACCOUNT: dict[str, int] = {
    "device_farm": 48,   # 8 天 × (1 次登录 + 5 次领券)
    "stealth": 42,       # 6 天 × (1 次登录 + 6 次领券)
    "refund_fraud": 51,  # 17 个"下单→支付→退款"循环
}


def _fraud_budget(profile: str) -> int:
    """一个作弊账号计划执行的**作弊动作**数（总预算扣掉掩护行为）。"""
    total = _CHEAT_EVENTS_PER_ACCOUNT[profile]
    return max(1, round(total * (1 - _CHEAT_COVER_SHARE[profile])))


def _cover_budget(profile: str) -> int:
    """一个作弊账号计划执行的**掩护行为**数（普通浏览会话）。"""
    return max(1, _CHEAT_EVENTS_PER_ACCOUNT[profile] - _fraud_budget(profile))


@dataclass(frozen=True)
class DatasetSpec:
    """生成参数。默认值即 PRD §14.1 的目标规模。

    ``anchor`` 是时间线的**右端点**（最晚一条事件的时刻），留空表示"现在"，
    即"最近 ``days`` 天"。

    **为什么必须能显式指定**：PRD §14.1 要求"随机种子固定 → 结果可复现"，
    但右端点若取自 ``utcnow()``，同一种子在不同日期、不同时段重跑会得到不同的
    绝对时间。``night_activity_ratio`` 这类特征**按小时**切分，事件落在几点会
    随运行时刻漂移，分布与模型指标因此都不可复现（种子只能保证"相对时间结构"
    一致）。指定 ``anchor`` 之后"同一 seed + 同一 anchor = 同一份数据集"才成立；
    演示需要"最近 14 天"的口径时再留空。
    """

    events: int = 50_000
    days: int = 14
    accounts: int = 800
    cheat_accounts: int = 70
    seed: int = 20260923
    batch_size: int = 200
    anchor: datetime | None = None


@dataclass(frozen=True)
class Action:
    """时间线上的一个待执行动作。

    先生成整条时间线再统一执行，是为了让事件**严格按时间顺序**进入链路：
    滑动窗口特征依赖"当时已发生的事件"，乱序执行会让特征看到未来数据 ——
    这在训练集里表现为"某条事件的特征包含了它之后才发生的行为"，
    模型会学到线上根本不存在的规律（特征泄漏）。
    """

    kind: str
    occurred_at: datetime
    actor: Actor
    amount: float = 0.0


@dataclass
class DatasetReport:
    """生成结果与分布核对。"""

    spec: DatasetSpec
    accounts: int = 0
    cheat_accounts: int = 0
    events: int = 0
    cheat_events: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_action: dict[str, int] = field(default_factory=dict)
    skipped: int = 0
    elapsed_seconds: float = 0.0

    def cheat_ratio(self) -> float:
        return self.cheat_events / self.events if self.events else 0.0

    def type_ratio(self, event_type: str) -> float:
        return self.by_type.get(event_type, 0) / self.events if self.events else 0.0

    def problems(self) -> list[str]:
        """返回与 PRD §14.1 口径的偏离（空列表 = 符合预期）。"""
        issues: list[str] = []
        low, high = EXPECTED_CHEAT_RATIO
        ratio = self.cheat_ratio()
        if not low <= ratio <= high:
            issues.append(
                f"作弊事件占比 {ratio:.2%} 超出预期区间 {low:.0%}~{high:.0%}："
                f"请调整 --cheat-accounts（当前 {self.spec.cheat_accounts}）"
            )
        for event_type, expected in EXPECTED_TYPE_MIX.items():
            actual = self.type_ratio(event_type)
            if abs(actual - expected) > TYPE_MIX_TOLERANCE:
                issues.append(
                    f"{event_type} 占比 {actual:.2%} 偏离目标 {expected:.0%} 超过 "
                    f"{TYPE_MIX_TOLERANCE:.0%}：会话模板需要重新标定"
                )
        return issues

    def summary(self) -> str:
        mix = "、".join(
            f"{name} {self.type_ratio(name):.1%}" for name in EXPECTED_TYPE_MIX
        )
        return (
            f"账号 {self.accounts} 个（作弊 {self.cheat_accounts} 个）｜"
            f"事件 {self.events} 条（作弊 {self.cheat_events} 条 = {self.cheat_ratio():.2%}）｜"
            f"跳过 {self.skipped} 个动作｜耗时 {self.elapsed_seconds:.1f}s\n"
            f"  事件类型分布：{mix}"
        )


# --------------------------------------------------------------------------- #
# 时间线规划
# --------------------------------------------------------------------------- #
def _split_cheat_profiles(cheat_accounts: int) -> dict[str, int]:
    """把作弊账号数按画像占比分配，余数给 ``refund_fraud``。

    取整误差全部记到退款欺诈上：它是三类里业务上最"重"的（单账号事件最多、
    涉及金额最大），少一个账号对样本分布的影响比少一个农场账号更明显。
    """
    counts: dict[str, int] = {}
    remaining = cheat_accounts
    for index, (profile, share) in enumerate(_CHEAT_PROFILE_SHARE):
        if index == len(_CHEAT_PROFILE_SHARE) - 1:
            counts[profile] = max(0, remaining)
            break
        value = int(round(cheat_accounts * share))
        counts[profile] = value
        remaining -= value
    return counts


def _ring_groups(accounts: int, *, per_group: int = 5) -> int:
    """退款团伙分成几个收货点（每组 per_group 人，至少 1 组）。"""
    return max(1, round(accounts / per_group))


def _build_cheat_actors(rng: random.Random, spec: DatasetSpec, *, batch: int) -> dict[str, list[Actor]]:
    """构造三类作弊账号（设备农场 / 隐蔽型 / 恶意退款）。

    退款欺诈账号的收货地址**按 3 人一组分配**，而不是全场共用一个：

    * 业务上，一个退款团伙会准备多个收货点，不会把 20 多个账号全铺在一个地址；
    * 数据上，"全场共用一个地址"会让 ``address_account_cnt_7d`` 变成一个
      一刀切的判别器（作弊 = 21、正常 = 1），模型只要看这一个特征就能满分 ——
      实测该特征权重曾达到 +3.74，是 AUC=1.0 的主要来源。
      改成 3 人一组后，地址维度的聚集度与"合租/家庭"的正常账号（4~6 人）重叠，
      模型必须结合退款率、金额、频次才能判断，这才是真实风控的形态。
    """
    counts = _split_cheat_profiles(spec.cheat_accounts)
    # 农场只准备 **3 台**设备（每台挂 8 个上下的账号）：农场主的成本结构就是
    # "一台真机/云机 + 一批账号"，不会给每个账号配一台机器。
    #
    # 这个数字还有一个模型侧的作用：``device_account_cnt_24h`` 若与正常
    # "合租/家庭"组（3~8 个账号共用）完全重叠，逻辑回归会因为两类都有高值
    # 而给出**负权重**（"同设备账号越多越不可疑"）—— 权重方向荒谬，
    # 演示里的"贡献度 Top5"就成了解释不通的一栏。
    pool = build_device_pool(3)
    # 团伙规模固定 5 人（末组略少），与正常"合租"组的 4~6 人区间重叠 ——
    # 具体分几组由 `_ring_groups` 决定，不写死数字，避免改动画像配比后
    # 两个分布悄悄错开（错开了就等于又给模型塞了一个便宜的判别器）。
    ring_groups = _ring_groups(counts["refund_fraud"])
    actors: dict[str, list[Actor]] = {}
    index = batch
    for profile in ("device_farm", "stealth", "refund_fraud"):
        group: list[Actor] = []
        for offset in range(counts[profile]):
            if profile == "device_farm":
                group.append(
                    build_actor(
                        rng,
                        index=index,
                        profile="device_farm",
                        device_pool=pool,
                        # 每 5 个农场账号里约 2 个直接挂家宽出口。
                        # 全都走代理 IP 的话，``ip_is_datacenter`` 会等价于"是农场"。
                        home_ip=offset % 5 < 2,
                    )
                )
            elif profile == "stealth":
                group.append(
                    build_actor(rng, index=index, profile="device_farm", stealth=True)
                )
            else:
                group.append(
                    build_actor(
                        rng,
                        index=index,
                        profile="refund_fraud",
                        address_hash=f"ADDR-DS-FRAUD-{batch}-{offset % ring_groups}",
                    )
                )
            index += 1
        actors[profile] = group
    return actors


def _plan_cheat_actions(
    rng: random.Random,
    actors_by_profile: dict[str, list[Actor]],
    *,
    start: datetime,
    end: datetime,
) -> list[Action]:
    """按画像生成作弊账号的动作计划。

    三类画像的**时间形态刻意不同**，否则模型只会学到"某个账号事件多"：

    * 设备农场 / 隐蔽型：集中爆发（同一小时内的连续领券），对应"频次"类特征；
    * 恶意退款：老账号 + 正常购物节奏，风险只在售后环节显现，对应"行为序列"特征。
    """
    actions: list[Action] = []
    # 预留 3 小时：退款动作挂在支付之后 2 小时，窗口末尾的会话必须留得下它，
    # 否则会被裁掉，样本里就会出现"只有订单没有退款"的假阴性。
    latest = end - timedelta(hours=3)

    # 设备农场：**按设备分组排班**，同组账号在同一个时段开工。
    #
    # 农场主的真实操作方式是"拿起一台手机，把这台手机上的账号挨个刷一遍"，
    # 同一台设备上的账号因此天然聚集在同一小时里。这一点直接决定
    # ``device_account_cnt_24h`` / ``device_coupon_cnt_1h`` 能不能识别出农场 ——
    # 若让同设备的账号各自随机散落到 14 天里，任意 24 小时窗口内每台设备
    # 只出现一个账号，"同设备多账号"这个信号会自己消失，规则和模型都看不到。
    #
    # 时间线铺开到约 12 天（每轮间隔 34~40 小时，共 8 轮）：账号年龄因此落在
    # 0~12 天，与"刚注册的正常新用户"区间重叠，``account_age_days``
    # 单独不再能把作弊账号分干净 —— 真实黑产养号会持续数周，
    # 账号年龄与正常新用户本来就没有本质差别。
    #
    # 注册时刻取在**首轮开工前 1~6 小时**：这一步不能省。若注册时刻随机散落，
    # 必然出现"攻击时刻早于注册时刻"的账号，``account_age_days`` 会算出负年龄
    # 并被记为缺失 —— 生成器写出时间线上的自我矛盾，表现为"少量样本特征缺失"，
    # 不逐个核对账号几乎不可能发现（详见 docs/ARCHITECTURE.md 的踩坑记录）。
    for group in _group_by_device(actors_by_profile.get("device_farm", [])):
        cursor = _random_moment(rng, start, latest - timedelta(days=13))
        cursor += timedelta(hours=rng.randint(1, 6))
        for actor in group:
            actions.append(Action("register", cursor - timedelta(hours=rng.randint(1, 6)), actor))
        # 每轮每账号固定 6 条作弊事件（1 次登录 + 5 张券），轮数与预算对齐
        for _ in range(max(1, _fraud_budget("device_farm") // 6)):
            for actor in group:
                started = cursor + timedelta(minutes=rng.randint(0, 90))
                actions.append(Action("login", started, actor))
                for offset in range(5):
                    actions.append(
                        Action(
                            "coupon_receive",
                            started + timedelta(minutes=2 * offset),
                            actor,
                            _coupon_value(rng, _BIG_COUPON_VALUES),
                        )
                    )
            cursor += timedelta(days=2, hours=rng.randint(0, 12))
        # 掩护行为：农场账号也会像普通用户一样逛一逛、下几单。
        # 没有这部分，`user_order_cnt_1h == 0` 就成了"作弊账号"的专属指纹。
        for actor in group:
            actions.extend(
                _plan_sessions(
                    rng,
                    actor,
                    start=start,
                    end=end,
                    budget=_cover_budget("device_farm"),
                )
            )

    for actor in actors_by_profile.get("stealth", []):
        # 隐蔽型：环境完全干净（独立设备、家宽、正常指纹），
        # 只能靠"高频领券 + 从不消费"这类行为组合识别。
        #
        # **券必须摊开领，不能 10 分钟内连领 6 张**：一小时连领 6 张券是
        # 一件"一眼假"的事，任何 1h 频次阈值都能抓住，模型学到的只是
        # 「6 > 3」这个数字，而不是行为模式。摊成"每小时 1~2 张、连着 5 小时"
        # 之后，单小时频次与正常领券党完全重叠，信号退化为**日累计领券量**，
        # 这才是真实风控里识别薅羊毛的真正依据。
        registered = _random_moment(rng, start, latest - timedelta(days=9))
        actions.append(Action("register", registered, actor))
        burst_cursor = registered + timedelta(hours=rng.randint(4, 12))
        for _ in range(max(1, _fraud_budget("stealth") // 7)):
            actions.append(Action("login", burst_cursor, actor))
            for step in range(6):
                actions.append(
                    Action(
                        "coupon_receive",
                        burst_cursor + timedelta(minutes=60 * step + rng.randint(5, 50)),
                        actor,
                        _coupon_value(rng, _BIG_COUPON_VALUES),
                    )
                )
            burst_cursor += timedelta(days=1, hours=rng.randint(0, 6))
        actions.extend(
            _plan_sessions(
                rng,
                actor,
                start=start,
                end=end,
                budget=_cover_budget("stealth"),
            )
        )

    for actor in actors_by_profile.get("refund_fraud", []):
        # 老账号：注册时间落在数据集窗口之前，"新账号"类规则不该命中它们
        actions.append(Action("register", start - timedelta(days=rng.randint(60, 400)), actor))
        days = sorted(rng.sample(range(_days_span(start, end)), k=min(6, _days_span(start, end))))
        cycles = max(1, _fraud_budget("refund_fraud") // 3)
        for index in range(cycles):
            day = days[index % len(days)]
            moment = _moment_on_day(rng, start, day, hour_range=(9, 20), latest=latest)
            amount = float(rng.randrange(*_FRAUD_ORDER_AMOUNT))
            actions.append(Action("order_create", moment, actor, amount))
            actions.append(Action("order_pay", moment + timedelta(minutes=3), actor, amount))
            actions.append(
                Action("after_sale_apply", moment + timedelta(hours=2), actor, amount)
            )
        actions.extend(
            _plan_sessions(
                rng,
                actor,
                start=start,
                end=end,
                budget=_cover_budget("refund_fraud"),
            )
        )
    return actions


def _days_span(start: datetime, end: datetime) -> int:
    """窗口天数（至少 1，避免 ``rng.sample(range(0))`` 这种边界崩溃）。"""
    return max(1, (end.date() - start.date()).days)


def _group_by_device(actors: list[Actor]) -> list[list[Actor]]:
    """按"共用同一台设备"给账号分组（保持首次出现顺序，保证可复现）。

    农场的一台设备上挂着多个账号，它们的开工时刻必须绑定在一起 ——
    原因见 ``_plan_cheat_actions`` 里对设备排班的说明。
    """
    groups: dict[str, list[Actor]] = {}
    for actor in actors:
        groups.setdefault(actor.device.device_id, []).append(actor)
    return list(groups.values())


def _random_moment(rng: random.Random, start: datetime, latest: datetime) -> datetime:
    """在 ``[start, latest]`` 内随机取一个时刻。"""
    span = int((latest - start).total_seconds())
    return start + timedelta(seconds=rng.randrange(max(1, span)))


def _moment_on_day(
    rng: random.Random,
    start: datetime,
    day_offset: int,
    *,
    hour_range: tuple[int, int],
    latest: datetime,
) -> datetime:
    """取"第 N 天的某个工作时段"的时刻，并保证不越过 ``latest``。"""
    base = start + timedelta(days=day_offset)
    moment = base.replace(
        hour=rng.randrange(*hour_range), minute=rng.randrange(60), second=rng.randrange(60)
    )
    return min(moment, latest)


def _plan_normal_actions(
    rng: random.Random,
    actor: Actor,
    *,
    start: datetime,
    end: datetime,
    budget: int,
    register_days: int | None = None,
    persona: str = _PERSONA_NORMAL,
) -> list[Action]:
    """按会话模板生成一个正常账号的动作计划。

    预算是**按会话粒度**收敛的：一个会话一旦开始就完整走完，不做"砍掉最后几条"的
    截断 —— 会话尾部恰好是支付与退款，截断会让样本里的售后事件系统性偏少，
    而这正是模型最需要学的一类。

    ``register_days`` 是"账号在数据集窗口开始前多少天注册"：
    省略时按 ``_REGISTER_AGE_BUCKETS`` 抽（新用户与老用户混合），
    共用设备的"家庭/合租"组会显式传入 1~6 天（家人刚换手机）。
    有了这个分布，``device_new_account_ratio_24h`` 与 ``account_age_days``
    在正常流量上就不再是恒定的"0 / 几百天"——恒定特征会让模型把
    "该特征有值"直接当成作弊标记。

    ``persona`` 区分两类**都合法**的用户：``normal``（偶尔领券）与
    ``hunter``（领券党，一次蹲券连领 3~7 张、偏好大额券）。
    领券党的行为在频次特征上与"隐蔽型薅羊毛账号"高度重叠 ——
    这正是数据集需要保留的模糊地带（见 ``_NORMAL_COUPON_HUNTER_SHARE``）。
    """
    if register_days is None:
        register_days = _random_register_days(rng)
    actions: list[Action] = [
        Action("register", start - timedelta(days=register_days), actor)
    ]
    actions.extend(
        _plan_sessions(
            rng,
            actor,
            start=start,
            end=end,
            budget=budget,
            persona=persona,
        )
    )
    return actions


def _plan_sessions(
    rng: random.Random,
    actor: Actor,
    *,
    start: datetime,
    end: datetime,
    budget: int,
    persona: str = _PERSONA_NORMAL,
) -> list[Action]:
    """生成若干个"浏览会话"的动作（**不含注册**）。

    独立于 ``_plan_normal_actions`` 是为了让作弊账号也能复用同一套会话逻辑 ——
    真实黑产账号会**养号**：一边刷券/退款，一边像普通用户那样浏览和下单。
    （作弊账号的"掩护行为"见 ``_CHEAT_COVER_SHARE``。）
    """
    actions: list[Action] = []
    coupon_prob = (
        _HUNTER_COUPON_SESSION_PROB
        if persona == _PERSONA_HUNTER
        else _SESSION_PROB["coupon_receive"]
    )
    latest = end - timedelta(hours=3)
    while len(actions) < budget:
        moment = _random_moment(rng, start, latest)
        actions.append(Action("login", moment, actor))
        offset = timedelta(minutes=rng.randint(1, 10))
        if rng.random() < coupon_prob:
            if persona == _PERSONA_HUNTER:
                # 蹲券：一次登录连续领好几张（间隔 2 分钟），单小时的领券数
                # 因此与隐蔽型作弊账号（一次 6 张）落在同一区间。
                coupon_count = rng.randint(*_HUNTER_COUPONS_PER_SESSION)
                coupon_table = _BIG_COUPON_VALUES
            else:
                coupon_count = 1
                coupon_table = _COUPON_VALUES
            grabbed_at = moment + offset
            for step in range(coupon_count):
                actions.append(
                    Action(
                        "coupon_receive",
                        grabbed_at + timedelta(minutes=2 * step),
                        actor,
                        _coupon_value(rng, coupon_table),
                    )
                )
        if rng.random() < _SESSION_PROB["order_create"]:
            amount = _order_amount(rng)
            ordered_at = moment + offset + timedelta(minutes=rng.randint(1, 5))
            actions.append(Action("order_create", ordered_at, actor, amount))
            if rng.random() < _SESSION_PROB["order_pay"]:
                paid_at = ordered_at + timedelta(minutes=rng.randint(1, 5))
                actions.append(Action("order_pay", paid_at, actor, amount))
                if rng.random() < _SESSION_PROB["after_sale_apply"]:
                    actions.append(
                        Action(
                            "after_sale_apply",
                            min(paid_at + timedelta(hours=2), end),
                            actor,
                            amount,
                        )
                    )
    return actions


# 同一时刻的动作排序：注册 → 登录 → 领券 → 下单 → 支付 → 退款。
# 必须确定，否则"支付"可能排在"下单"前面（两者时间戳可能相同），
# 执行时会因为"订单不存在"被业务校验拒绝，样本里凭空少掉一条事件。
_KIND_ORDER: dict[str, int] = {
    "register": 0,
    "login": 1,
    "coupon_receive": 2,
    "order_create": 3,
    "order_pay": 4,
    "after_sale_apply": 5,
}


def _sort_key(action: Action) -> tuple[datetime, int]:
    return (action.occurred_at, _KIND_ORDER[action.kind])


# --------------------------------------------------------------------------- #
# 时间线规划入口
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DatasetPlan:
    """规划结果（纯内存，尚未落库）。"""

    actions: list[Action]
    start: datetime
    end: datetime
    cheat_accounts: int
    normal_accounts: int
    cheat_events: int

    @property
    def accounts(self) -> int:
        return self.cheat_accounts + self.normal_accounts

    def type_mix(self) -> dict[str, int]:
        """规划阶段的事件类型计数（不含注册）。"""
        mix: dict[str, int] = defaultdict(int)
        for action in self.actions:
            if action.kind != "register":
                mix[action.kind] += 1
        return dict(mix)


def plan_actions(spec: DatasetSpec, *, batch: int) -> DatasetPlan:
    """规划整条时间线（**纯计算，不落库**）。

    把规划从执行里拆出来，是为了能在秒级核对事件类型分布：
    全量落库的瓶颈是 5 万次事件网关调用（约 17 分钟），
    调一次会话模板就等 17 分钟是不可接受的；而分布是否满足 PRD §14.1
    完全由规划阶段决定，与落库无关。

    时间线右端点取 ``spec.anchor``（留空才用当前时刻），这样"同 seed 同 anchor
    必得同一份时间线"，可复现性不依赖跑脚本的钟点（见 ``DatasetSpec.anchor``）。
    """
    rng = random.Random(spec.seed)
    end = spec.anchor or utcnow()
    start = end - timedelta(days=spec.days)

    actors_by_profile = _build_cheat_actors(rng, spec, batch=batch)
    cheat_actions = _plan_cheat_actions(rng, actors_by_profile, start=start, end=end)
    cheat_accounts = sum(len(group) for group in actors_by_profile.values())
    cheat_events = sum(1 for action in cheat_actions if action.kind != "register")

    normal_accounts = max(0, spec.accounts - cheat_accounts)
    budget_total = max(0, spec.events - cheat_events)
    per_account = round(budget_total / normal_accounts) if normal_accounts else 0

    # 正常账号里混入一部分"环境可疑但其实正常"的用户：共用设备 + 共用地址。
    # 每 4~6 个这样的用户共用一台设备/一个地址，模拟家庭成员与合租同事。
    normal_actions: list[Action] = []
    shared_total = int(round(normal_accounts * _NORMAL_SUSPICIOUS_SHARE))
    shared_done = 0
    shared_pool_index = 0
    while shared_done < shared_total:
        # 组大小 3~8 且分布很宽：合租房的住户数、家庭成员数本来就差异很大。
        # 与农场设备（约 8 个账号）**在上界处重叠**，这样 `device_account_cnt_24h`
        # 就只能是"需要核实的信号"而不是"农场专属指纹"。
        group_size = min(rng.randint(3, 8), shared_total - shared_done)
        shared_device = Device(
            device_id=f"D-HOME-{batch}-{shared_pool_index}",
            fingerprint=dict(rng.choice(NORMAL_FINGERPRINTS)),
        )
        shared_address = f"ADDR-HOME-{batch}-{shared_pool_index}"
        shared_pool_index += 1
        for _ in range(group_size):
            # 组里每 4 人掺一个"刚注册的新成员"：让"共用设备"与"新账号"
            # 这两个信号在数据里可以独立出现，而不是绑死在一起
            # （绑死之后模型会学成"共用设备 = 作弊"，正是最贵的那类误拦）。
            register_days = (
                rng.randint(*_NEW_MEMBER_AGE_DAYS)
                if shared_done % _NEW_MEMBER_EVERY == 0
                else None
            )
            actor = build_shared_home_actor(
                rng,
                index=batch + 1000 + shared_done,
                shared_device=shared_device,
                shared_address=shared_address,
            )
            normal_actions.extend(
                _plan_normal_actions(
                    rng,
                    actor,
                    start=start,
                    end=end,
                    budget=per_account,
                    register_days=register_days,
                )
            )
            shared_done += 1

    # 剩下的正常账号里再分出两类"行为上难辨"的合法用户：
    #   * 半可疑环境（Root / 云手机 / 多开 / 改定位）：让 device_env_risk 不再是二值；
    #   * 领券党（一次蹲券连领多张、偏好大额券）：让频次类特征不再是二值。
    # 二者都是为了让"单特征一刀切"不可能成立（见各自常量的说明）。
    risky_env_total = int(round(normal_accounts * _NORMAL_RISKY_ENV_SHARE))
    hunter_total = int(round(normal_accounts * _NORMAL_COUPON_HUNTER_SHARE))
    vpn_total = int(round(normal_accounts * _NORMAL_VPN_SHARE))
    risky_env_done = 0
    hunter_done = 0
    vpn_done = 0
    for offset in range(shared_total, normal_accounts):
        if risky_env_done < risky_env_total:
            actor = build_actor(
                rng, index=batch + 1000 + offset, profile="normal", risky_env=True
            )
            risky_env_done += 1
            persona = _PERSONA_NORMAL
        elif vpn_done < vpn_total:
            actor = build_actor(
                rng, index=batch + 1000 + offset, profile="normal", vpn=True
            )
            vpn_done += 1
            persona = _PERSONA_NORMAL
        else:
            actor = build_actor(rng, index=batch + 1000 + offset, profile="normal")
            persona = _PERSONA_HUNTER if hunter_done < hunter_total else _PERSONA_NORMAL
            hunter_done += 1
        normal_actions.extend(
            _plan_normal_actions(
                rng,
                actor,
                start=start,
                end=end,
                budget=per_account,
                persona=persona,
            )
        )

    return DatasetPlan(
        actions=sorted(cheat_actions + normal_actions, key=_sort_key),
        start=start,
        end=end,
        cheat_accounts=cheat_accounts,
        normal_accounts=normal_accounts,
        cheat_events=cheat_events,
    )


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
class _Runner:
    """按时间线执行动作，并给 rc_event 回填训练标签。

    **标签为什么要"回填"而不是直接写**：标签是生成器才知道的元信息
    （这个账号是不是作弊账号），而事件行是由事件网关按事件契约写入的。
    让网关替生成器写标签，等于把"离线训练"的概念塞进线上接入路径；
    因此生成器在批量提交前用一条 UPDATE 补齐本批次的标签 ——
    同一事务内完成，不存在"事件提交了但标签没写上"的中间态。
    """

    def __init__(
        self,
        db: Session,
        *,
        spec: DatasetSpec,
        progress_every: int,
    ) -> None:
        self.db = db
        self.spec = spec
        self.progress_every = progress_every
        # autocommit=False：提交时机由本类掌控（每 batch_size 条一次），
        # 而不是每条事件一次 —— 后者会把吞吐压在 40 事件/秒。
        self.sim = BusinessSimulator(db, autocommit=False)
        self.pending_labels: list[tuple[str, bool]] = []
        self.unpaid: dict[str, list[str]] = defaultdict(list)
        self.paid: dict[str, list[str]] = defaultdict(list)
        self.report = DatasetReport(spec=spec)

    # ---- 执行入口 ---- #
    def run(self, actions: list[Action]) -> DatasetReport:
        for index, action in enumerate(actions, start=1):
            result = self._execute(action)
            if result is not None:
                self._record(action, result)
            if index % self.spec.batch_size == 0:
                self._flush()
            if self.progress_every and index % self.progress_every == 0:
                logger.info(
                    "生成进度 %d/%d（事件 %d 条，跳过 %d）",
                    index,
                    len(actions),
                    self.report.events,
                    self.report.skipped,
                )
        self._flush()
        return self.report

    # ---- 单个动作 ---- #
    def _execute(self, action: Action) -> BusinessResult | None:
        actor = action.actor
        if action.kind == "register":
            self.sim.register(actor, occurred_at=action.occurred_at)
            return None
        if action.kind == "login":
            return self.sim.login(actor, occurred_at=action.occurred_at)
        if action.kind == "coupon_receive":
            return self.sim.receive_coupon(
                actor, face_value=action.amount, occurred_at=action.occurred_at
            )
        if action.kind == "order_create":
            result = self.sim.create_order(
                actor, amount=action.amount, occurred_at=action.occurred_at
            )
            if result.allowed and result.biz_no:
                self.unpaid[actor.user_id].append(result.biz_no)
            return result
        if action.kind == "order_pay":
            order_no = self._pop(self.unpaid[actor.user_id])
            if order_no is None:
                # 订单被风控拦下（或被裁剪），支付动作失去依赖：跳过并计数。
                # 静默跳过是不可接受的 —— "样本比计划少"必须能在报告里看到。
                self.report.skipped += 1
                return None
            result = self.sim.pay_order(actor, order_no=order_no, occurred_at=action.occurred_at)
            if result.allowed:
                self.paid[actor.user_id].append(order_no)
            return result
        if action.kind == "after_sale_apply":
            order_no = self._pop(self.paid[actor.user_id])
            if order_no is None:
                self.report.skipped += 1
                return None
            return self.sim.apply_refund(
                actor,
                order_no=order_no,
                refund_amount=action.amount,
                reason="未收到货",
                occurred_at=action.occurred_at,
            )
        raise ValueError(f"未知动作类型：{action.kind}")

    @staticmethod
    def _pop(queue: list[str]) -> str | None:
        """按 FIFO 取一个单据号（同一账号可能有多个在途订单）。"""
        return queue.pop(0) if queue else None

    def _record(self, action: Action, result: BusinessResult) -> None:
        decision = result.decision
        event_type = str(decision.get("event_type") or "")
        self.report.events += 1
        self.report.by_type[event_type] = self.report.by_type.get(event_type, 0) + 1
        self.report.by_action[action.kind] = self.report.by_action.get(action.kind, 0) + 1
        # 标签是**账号级**的：确认作弊账号产生的所有事件都算作弊样本
        # （口径见模块文档与 rc_event.is_cheat 的列注释）。
        if action.actor.is_cheater:
            self.report.cheat_events += 1
        event_id = decision.get("event_id")
        if event_id:
            self.pending_labels.append((str(event_id), action.actor.is_cheater))

    # ---- 批量提交 ---- #
    def _flush(self) -> None:
        """回填标签后提交本批次（一个事务：事件 + 业务单据 + 标签）。"""
        if self.pending_labels:
            cheat_ids = [event_id for event_id, flag in self.pending_labels if flag]
            normal_ids = [event_id for event_id, flag in self.pending_labels if not flag]
            for ids, value in ((cheat_ids, True), (normal_ids, False)):
                if not ids:
                    continue
                self.db.execute(
                    update(RcEvent).where(RcEvent.event_id.in_(ids)).values(is_cheat=value),
                    execution_options={"synchronize_session": False},
                )
            self.pending_labels.clear()
        self.db.commit()


def generate(
    db: Session,
    *,
    spec: DatasetSpec,
    batch: int | None = None,
    progress_every: int = 5000,
) -> DatasetReport:
    """生成数据集（调用方负责建库/迁移/种子，本函数只负责数据）。

    ``batch`` 是账号号段基数：与场景脚本一样按秒分配，避免重复生成时
    与上一批账号撞号（撞号会让"新账号"画像失效，见 scenarios._batch_index）。
    """
    from app.simulator.scenarios import _batch_index

    started = time.perf_counter()
    batch = batch if batch is not None else _batch_index()
    plan = plan_actions(spec, batch=batch)
    logger.info(
        "时间线规划完成：%d 个动作（事件 %d 条 + 注册 %d 次），开始执行",
        len(plan.actions),
        sum(plan.type_mix().values()),
        plan.accounts,
    )

    runner = _Runner(db, spec=spec, progress_every=progress_every)
    report = runner.run(plan.actions)
    report.accounts = plan.accounts
    report.cheat_accounts = plan.cheat_accounts
    report.elapsed_seconds = time.perf_counter() - started
    return report
