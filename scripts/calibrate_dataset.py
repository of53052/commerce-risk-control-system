"""数据集分布标定（**纯规划、不落库**，秒级出结果）。

用法（在仓库根目录执行）::

    backend/.venv/Scripts/python.exe scripts/calibrate_dataset.py
    backend/.venv/Scripts/python.exe scripts/calibrate_dataset.py --events 50000 --cheat-accounts 70

**为什么需要它**：全量生成 5 万事件约 20 分钟，而"五类事件占比、作弊事件占比
是否满足 PRD §14.1"完全由 ``plan_actions``（纯计算阶段）决定 —— 与落库无关。
调一次会话模板就等 20 分钟是不可接受的，所以先用本脚本秒级核对，
达标了再去 ``scripts/gen_dataset.py`` 落库。

输出四块：① 规划总量与账号数；② 作弊事件占比（区间口径 6%~8%）；
③ 五类事件占比与容差对照；④ 结论（供脚本判断的退出码：0 = 达标，1 = 有指标超容差）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 标定用的账号号段基数：刻意取一个远离"按秒分配"的区间，避免与真实生成、
# 场景脚本的账号号段重叠（重叠会让"新账号"画像的判断失真）。
CALIBRATION_BATCH = 900_000


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="数据集分布标定（纯规划）")
    parser.add_argument("--events", type=int, default=50_000, help="目标事件总量")
    parser.add_argument("--days", type=int, default=14, help="时间跨度天数")
    parser.add_argument("--accounts", type=int, default=800, help="账号总数")
    parser.add_argument("--cheat-accounts", type=int, default=70, help="作弊账号数")
    parser.add_argument("--seed", type=int, default=20260923, help="随机种子")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    from app.simulator.dataset import (
        EXPECTED_TYPE_MIX,
        TYPE_MIX_TOLERANCE,
        DatasetSpec,
        plan_actions,
    )

    spec = DatasetSpec(
        events=args.events,
        days=args.days,
        accounts=args.accounts,
        cheat_accounts=args.cheat_accounts,
        seed=args.seed,
    )
    plan = plan_actions(spec, batch=CALIBRATION_BATCH)
    mix = plan.type_mix()
    total = sum(mix.values())

    print(
        f"事件总数(规划) {total}｜事件配额 {spec.events}｜"
        f"账号 {plan.accounts}（作弊 {plan.cheat_accounts}）"
    )
    print(
        f"作弊事件占比 {plan.cheat_events / total:.4f}"
        f"（目标 0.06~0.08，账号数决定的**涌现**结果，不在循环内调参）"
    )

    offenders: list[str] = []
    for name, target in EXPECTED_TYPE_MIX.items():
        actual = mix.get(name, 0) / total
        over = abs(actual - target) > TYPE_MIX_TOLERANCE
        print(
            f"  {name:18s} {actual:.4f}  目标 {target}"
            f"{'  <== 超容差' if over else ''}"
        )
        if over:
            offenders.append(name)

    print(
        "结论：分布达标（可落库）"
        if not offenders
        else f"结论：需要重新标定 —— {offenders}（改 _SESSION_TEMPLATE 后重跑本脚本）"
    )
    return 1 if offenders else 0


if __name__ == "__main__":
    raise SystemExit(main())