"""模拟业务端命令行入口：``python -m app.simulator --scenario coupon_farm``。

退出码约定（供 ``scripts/verify_p0.ps1`` 判定）：

* ``0``：场景执行完成且符合预期；
* ``1``：执行完成但**未满足预期**（例如该出现的 Reject 没出现）——
  这类失败意味着策略或链路出了问题，必须让脚本/CI 感知到；
* ``2``：执行过程中抛异常（数据库不可用、事件被业务校验拒绝等）。
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.simulator.scenarios import SCENARIOS, ScenarioReport


def _print_report(report: ScenarioReport, *, verbose: bool) -> None:
    print(f"\n===== {report.title} =====")
    if verbose:
        for index, step in enumerate(report.steps, start=1):
            decision = step.decision
            hits = "、".join(hit["rule_code"] for hit in decision.get("rule_hits") or []) or "-"
            print(
                f"  {index:3d}. {decision.get('event_type'):16s} {decision.get('user_id'):10s} "
                f"→ {step.action:9s} risk={decision.get('risk_score'):3d} "
                f"rule={decision.get('rule_score'):3d} model={decision.get('model_score'):3d} "
                f"biz_no={step.biz_no or '-':16s} 命中={hits}"
            )
    print(report.summary())
    problems = report.check()
    if problems:
        print("  ⚠️ 未满足预期：")
        for problem in problems:
            print(f"     - {problem}")
    else:
        print("  ✅ 场景符合预期")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="电商风控 · 模拟业务端")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=[*SCENARIOS.keys(), "all"],
        help="要执行的场景（默认全部）",
    )
    parser.add_argument("--seed", type=int, default=20260923, help="随机种子（固定即可复现）")
    parser.add_argument("--accounts", type=int, default=40, help="羊毛党场景的账号数")
    parser.add_argument("--users", type=int, default=30, help="正常流量场景的用户数")
    parser.add_argument("--verbose", action="store_true", help="逐条打印决策明细")
    args = parser.parse_args(argv)

    setup_logging()
    logging.getLogger("app.simulator").setLevel(logging.WARNING)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    failed = 0
    try:
        with SessionLocal() as db:
            for name in names:
                kwargs: dict = {"seed": args.seed}
                if name == "coupon_farm":
                    kwargs["accounts"] = args.accounts
                elif name == "normal_day":
                    kwargs["users"] = args.users
                report = SCENARIOS[name](db, **kwargs)
                _print_report(report, verbose=args.verbose)
                failed += len(report.check())
    except Exception as exc:  # noqa: BLE001 - CLI 入口需要把异常转成退出码
        print(f"场景执行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
