"""作弊场景脚本（docs/PRD.md §14.2 / §14.3）。

场景的价值不在"跑通"，而在**可复现地跑出预期结论**：每个场景都给出
``expected`` 断言（期望出现的动作与命中的规则类别），由 ``__main__`` 与
验收脚本据此判定"演示是否成功"。否则演示只是"看日志感觉对了"，
策略微调导致场景失效时没人会发现。

两个场景的攻击面刻意不同：

* **羊毛党**（§14.2）：靠**环境 + 频次**识别（设备农场、代理 IP、聚集度）；
* **恶意退款**（§14.3）：环境干净，只能靠**行为序列 + 收货地址关联**识别。

只用其中一个场景演示，很容易得出"规则够用"或"模型没用"的错误结论。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.simulator.actors import Actor, build_actor, build_device_pool
from app.simulator.service import BusinessResult, BusinessSimulator


def _batch_index(unit: int = 1000) -> int:
    """生成本次运行的账号号段基数（按秒钟递增）。

    **为什么不能固定号段**：账号 ID 会进入条带（``subject`` 段）与业务表。
    若两次运行都用 U900000 起头，第二次运行看到的账号就是"6 分钟前已注册过"
    的老账号，``account_age_days`` 不再为 0，新账号相关规则静默失效 ——
    同一份脚本第一次跑出 Reject、第二次跑出 Pass，演示就失去了可信度。

    取秒钟时间戳再乘 ``unit``，既保证每次运行号段不同，又让账号 ID
    与"运行时刻"有可读的对应关系（``prefix_index`` 仍可显式覆盖，供测试固定）。
    """
    return int(time.time()) * unit


@dataclass
class ScenarioReport:
    """场景执行报告。"""

    name: str
    title: str
    steps: list[BusinessResult] = field(default_factory=list)
    accounts: int = 0
    rejected: int = 0
    reviewed: int = 0
    challenged: int = 0
    passed: int = 0
    hit_rules: dict[str, int] = field(default_factory=dict)
    expected: list[str] = field(default_factory=list)
    # 期望命中的规则号。动作断言只回答"结果对不对"，规则断言才回答
    # "是不是预期的那条链路在起作用" —— 少了它，某条规则被改坏后由另一条规则
    # 误打误撞出同样的动作，场景照样"通过"，问题被演示结果掩盖。
    expect_rules: list[str] = field(default_factory=list)

    def add(self, result: BusinessResult) -> BusinessResult:
        self.steps.append(result)
        action = result.action
        if action == "Reject":
            self.rejected += 1
        elif action == "Review":
            self.reviewed += 1
        elif action == "Challenge":
            self.challenged += 1
        else:
            self.passed += 1
        for hit in result.decision.get("rule_hits") or []:
            code = str(hit.get("rule_code"))
            self.hit_rules[code] = self.hit_rules.get(code, 0) + 1
        return result

    def check(self) -> list[str]:
        """返回未满足的预期（空列表 = 场景符合预期）。"""
        problems: list[str] = []
        if "reject" in self.expected and self.rejected == 0:
            problems.append("预期出现 Reject，但没有任何动作被拒绝")
        if "review" in self.expected and self.reviewed == 0:
            problems.append("预期出现 Review，但没有动作落入人工审核区间")
        if "pass" in self.expected and self.passed == 0:
            problems.append("预期出现 Pass，但没有任何动作被直接放行")
        if "no_reject" in self.expected and self.rejected:
            # 正常流量基线：出现 Reject 说明规则过严（误拦），
            # 这比"漏拦"更难被业务方接受，必须让验收脚本卡住。
            problems.append(f"正常流量出现 {self.rejected} 次 Reject —— 疑似规则过严（误拦），需排查")
        for code in self.expect_rules:
            if code not in self.hit_rules:
                problems.append(f"预期命中规则 {code}，整场未命中 —— 规则、特征或阈值可能已失效")
        return problems

    def summary(self) -> str:
        top = sorted(self.hit_rules.items(), key=lambda item: -item[1])[:5]
        hits = "、".join(f"{code}×{count}" for code, count in top) or "无"
        return (
            f"{self.title}：账号 {self.accounts} 个，动作 {len(self.steps)} 次 → "
            f"Pass {self.passed} / Review {self.reviewed} / "
            f"Challenge {self.challenged} / Reject {self.rejected}；"
            f"命中规则 Top5：{hits}"
        )


def coupon_farm(
    db: Session,
    *,
    seed: int = 20260923,
    accounts: int = 40,
    devices: int = 5,
    rounds: int = 5,
    stealth_accounts: int = 2,
    prefix_index: int | None = None,
) -> ScenarioReport:
    """场景一：大促羊毛党批量领券（PRD §14.2）。

    剧本：攻击者用设备农场注册 ``accounts`` 个新账号，共用 ``devices`` 台设备与
    3 个代理 IP，集中领取高面额券。预期：随着同设备账号数上升，
    规则分累加至 Reject，且这些券**没有落 biz_coupon_receive**。

    另外混入 ``stealth_accounts`` 个"隐蔽型"账号（独立设备 + 家宽 + 干净指纹），
    每个号短时领券 ``rounds`` 次。它们只有单一弱信号（``RC_FREQ_002`` 20 分），
    因此**会被放行** —— 这是刻意保留的对照组：

    * 说明"环境特征全灭时，单条频次规则不足以定级"，模型的加权价值就在这个区间；
    * 给大盘提供非 100% 的拦截率，否则指标失去意义。

    本场景的预期是"出现 Reject"（农场账号），不把隐蔽账号被放行当成失败 ——
    它的判定在 ``stealth`` 相关的输出里单独体现。

    ``prefix_index`` 缺省时按秒钟自动分配（见 ``_batch_index``），
    避免重复运行场景时"上一次的账号还在条带与业务表里"导致两次结果不一致；
    测试可以显式传入固定号段以获得完全确定的账号 ID。
    """
    batch = prefix_index if prefix_index is not None else _batch_index()
    rng = random.Random(seed)
    sim = BusinessSimulator(db)
    report = ScenarioReport(
        name="coupon_farm",
        title="场景一 · 设备农场批量领券",
        expected=["reject"],
        # 断言"靠环境 + 同设备账号聚集识别"这条链路，而不是"只要被拦就算过"：
        # 农场账号同时命中多条环境与频次规则，任一条被改坏都还拦得住，
        # 但那就不是本场景要演示的识别路径了。
        expect_rules=["RC_ENV_001", "RC_FREQ_001"],
    )

    # ---- 对照组：隐蔽型账号（预期 Pass） ----
    for offset in range(stealth_accounts):
        actor = build_actor(
            rng,
            index=batch + 500 + offset,
            profile="device_farm",
            stealth=True,
        )
        sim.register(actor)
        report.accounts += 1
        report.add(sim.login(actor))
        for _ in range(rounds):
            report.add(sim.receive_coupon(actor, face_value=150.0, coupon_id="CP-BIG-150"))

    # ---- 主链路：设备农场（预期 Reject） ----
    pool = build_device_pool(devices)
    for offset in range(accounts):
        actor = build_actor(rng, index=batch + offset, profile="device_farm", device_pool=pool)
        sim.register(actor)
        report.accounts += 1
        report.add(sim.login(actor))
        for _ in range(rounds):
            report.add(sim.receive_coupon(actor, face_value=150.0))
    return report


def refund_fraud(
    db: Session,
    *,
    seed: int = 20260923,
    prior_accounts: int = 4,
    refund_count: int = 3,
    prefix_index: int | None = None,
) -> ScenarioReport:
    """场景二：高价值商品恶意退款欺诈（PRD §14.3）。

    剧本分两段，**顺序本身就是剧本的一部分**：

    1. **历史账号**（``prior_accounts`` 个）：6 天前各自用同一个收货地址下单并退款，
       制造"该地址 7 天内被多个账号用来退款"的聚集证据；
    2. **主账号**：先在 24 小时内下 ``refund_count`` 笔高额订单，再逐笔申请
       "未收到货"仅退款 —— **下单全部排在退款之前**。

    两段都不能省：历史账号提供 ``address_refund_cnt_7d`` 的聚集证据，否则主账号的退款
    只是"一个用户退了三笔"，构不成团伙画像；下单前置则是为了让
    ``user_first_order_refund``（首单即退款）为假 —— "首单就退"是试探型欺诈，
    与本场景要演示的"连续退款"是两种行为，混在一起规则命中就解释不清了。

    预期是**逐笔升级**：前两笔退款只有 ``RC_AMT_003``（金额）+ ``RC_AS_004``（地址聚集）
    支撑，还在放行区间；第 3 笔退款时 ``user_refund_cnt_24h`` 到达 3，命中
    ``RC_AS_002``，总分落进 60~79 → ``Review``。单笔证据不足、连续退款叠加
    金额与地址聚集才够格建案，这正是 PRD §14.3 要演示的判定分寸。

    账号注册时间刻意落在 60 天以前（``register(occurred_at=...)``）：PRD 给恶意退款
    账号的画像就是"老账号 + 干净环境"。若注册时间取当下，``RC_AMT_004``
    （新账号高额订单）会意外命中，把"行为序列"的演示变成"新账号"的演示。
    """
    rng = random.Random(seed)
    sim = BusinessSimulator(db)
    report = ScenarioReport(
        name="refund_fraud",
        title="场景二 · 高价值订单恶意退款",
        expected=["review"],
        expect_rules=["RC_AMT_003", "RC_AS_002", "RC_AS_004"],
    )

    batch = prefix_index if prefix_index is not None else _batch_index()
    shared_address = f"ADDR-FRAUD-{batch}"
    now = utcnow()

    # ---- 第一段：历史账号（6 天前，同地址各自下单 + 退款）----
    for offset in range(prior_accounts):
        actor = build_actor(
            rng, index=batch + offset, profile="refund_fraud", address_hash=shared_address
        )
        sim.register(actor, occurred_at=now - timedelta(days=rng.randint(60, 400)))
        report.accounts += 1
        past = now - timedelta(days=6, hours=offset)
        order = sim.create_order(actor, amount=3000.0, occurred_at=past)
        report.add(order)
        if not order.allowed:
            continue
        sim.pay_order(actor, order_no=order.biz_no, occurred_at=past + timedelta(minutes=5))
        report.add(sim.apply_refund(actor, order_no=order.biz_no, occurred_at=past + timedelta(hours=1)))

    # ---- 第二段：主账号（24 小时内下 refund_count 笔高额订单，随后逐笔退款）----
    main_actor = build_actor(
        rng, index=batch + 999, profile="refund_fraud", address_hash=shared_address
    )
    sim.register(main_actor, occurred_at=now - timedelta(days=rng.randint(60, 400)))
    report.accounts += 1

    order_nos: list[str] = []
    for index in range(refund_count):
        ordered_at = now - timedelta(hours=refund_count - index)
        order = sim.create_order(main_actor, amount=5000.0 + index * 1000, occurred_at=ordered_at)
        report.add(order)
        if not order.allowed:
            continue
        sim.pay_order(main_actor, order_no=order.biz_no, occurred_at=ordered_at + timedelta(minutes=3))
        order_nos.append(order.biz_no)

    for index, order_no in enumerate(order_nos):
        report.add(
            sim.apply_refund(
                main_actor,
                order_no=order_no,
                reason="未收到货",
                occurred_at=now - timedelta(minutes=(len(order_nos) - index) * 10),
            )
        )
    return report


def normal_day(
    db: Session,
    *,
    seed: int = 20260923,
    users: int = 30,
    prefix_index: int | None = None,
) -> ScenarioReport:
    """正常用户的一天：登录 → 领券 → 下单 → 支付。

    用途有两个：给大盘提供"正常流量"基线（否则拦截率永远是 100%，
    指标毫无意义），以及作为**误拦的回归基线** —— 正常流量里出现 Reject
    就是需要立刻排查的规则过严。
    """
    batch = prefix_index if prefix_index is not None else _batch_index()
    rng = random.Random(seed)
    sim = BusinessSimulator(db)
    report = ScenarioReport(
        name="normal_day",
        title="正常流量基线 · 登录/领券/下单/支付",
        # no_reject：正常流量出现拦截即为误拦，属于必须排查的回归
        expected=["pass", "no_reject"],
    )
    now = utcnow()
    for offset in range(users):
        actor = build_actor(rng, index=batch + offset, profile="normal")
        sim.register(actor, occurred_at=now - timedelta(days=rng.randint(30, 900)))
        report.accounts += 1
        moment = now - timedelta(minutes=rng.randint(1, 120))
        report.add(sim.login(actor, occurred_at=moment))
        if rng.random() < 0.6:
            report.add(
                sim.receive_coupon(actor, face_value=rng.choice([10.0, 20.0, 50.0]), occurred_at=moment)
            )
        if rng.random() < 0.5:
            order = sim.create_order(
                actor, amount=round(rng.uniform(50, 600), 2), occurred_at=moment + timedelta(minutes=2)
            )
            report.add(order)
            if order.allowed and rng.random() < 0.8:
                report.add(
                    sim.pay_order(actor, order_no=order.biz_no, occurred_at=moment + timedelta(minutes=4))
                )
    return report


SCENARIOS = {
    "coupon_farm": coupon_farm,
    "refund_fraud": refund_fraud,
    "normal_day": normal_day,
}
