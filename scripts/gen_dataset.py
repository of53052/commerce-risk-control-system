"""数据集生成脚本（docs/PRD.md §14.1）。

用法（在仓库根目录执行）::

    backend/.venv/Scripts/python.exe scripts/gen_dataset.py                # 5 万事件
    backend/.venv/Scripts/python.exe scripts/gen_dataset.py --limit 2000   # 冒烟
    backend/.venv/Scripts/python.exe scripts/gen_dataset.py --reset        # 重建数据集库
    backend/.venv/Scripts/python.exe scripts/gen_dataset.py --end 2026-09-23T12:00:00Z

**可复现性要固定两个参数**：分布由 ``--seed`` 决定，时间线还取决于 ``--end``
（右端点）。``--end`` 留空时右端 = 运行时刻，于是"同一 seed 在不同日期/时段重跑"
会得到不同的绝对时间，而 ``night_activity_ratio`` 这类特征按**小时**切分，
事件落在几点会直接改变特征值与模型指标。要复跑出同一份数据，seed 与 end 都要写死。

**建表走 alembic 迁移**（``app.db.migrate.upgrade_to_head``）：数据集库必须与
开发库同构，否则迁移里用 ``op.execute`` 写的触发器/索引在数据集库里根本不存在。
因此旧的 ``create_all`` 库需要先 ``--reset`` 一次（迁移无法叠加在同类表结构上）。

**为什么要单独的库**：生成 5 万条事件会写入 5 万个决策、5 万份特征快照与大量业务单据。
如果写进开发库 ``risk_control``，演示时的大盘拦截率、案件列表、Redis 窗口条带
全都会被这批数据污染 —— 表现为"第一次演示正常、第二次分数偏高"，
而且很难看出是数据问题。专用库可以随时 ``--reset`` 重建，不需要人工清理。

**为什么连 Redis 也要换 DB**：特征窗口条带在 Redis 里。用同一个 DB 时，
生成过程会把条带写满，演示时的频次特征会因为"历史残留"而虚高。
脚本默认把 REDIS_DB 指向 2（开发 0 / 测试 1 / 数据集 2）。

**退出码**：0 = 生成完成且分布符合 PRD 口径；1 = 生成完成但分布偏离；2 = 执行异常。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# 让脚本能直接 import app.*：把 backend/ 放进 sys.path，与 alembic.ini 的定位方式一致。
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

DATASET_DB_NAME = "risk_control_dataset"
DATASET_REDIS_DB = 2


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="风控数据集生成（PRD §14.1）")
    parser.add_argument("--limit", type=int, default=50_000, help="目标事件总量（默认 50000）")
    parser.add_argument("--days", type=int, default=14, help="时间跨度天数（默认 14）")
    parser.add_argument("--accounts", type=int, default=800, help="账号总数（默认 800）")
    parser.add_argument("--cheat-accounts", type=int, default=70, help="作弊账号数（默认 70）")
    parser.add_argument("--seed", type=int, default=20260923, help="随机种子（默认固定）")
    parser.add_argument(
        "--end",
        default=None,
        help="时间线右端点（ISO8601，如 2026-09-23T12:00:00Z）；留空=当前时刻",
    )
    parser.add_argument("--batch-size", type=int, default=200, help="每多少条事件提交一次")
    parser.add_argument("--db-name", default=DATASET_DB_NAME, help="目标库名")
    parser.add_argument("--redis-db", type=int, default=DATASET_REDIS_DB, help="Redis DB")
    parser.add_argument("--reset", action="store_true", help="先 DROP 目标库再重建（破坏性）")
    parser.add_argument("--keep-events", action="store_true", help="追加而不清空已有数据")
    parser.add_argument("--batch", type=int, default=None, help="账号号段基数（默认按秒分配）")
    return parser.parse_args(argv)


def _configure_env(args: argparse.Namespace) -> None:
    """在 import app.* 之前把环境变量指到数据集库。

    必须在导入之前做：``app.core.config.settings`` 是模块级单例，
    导入即读值，之后再改环境变量不会生效（与 tests/conftest.py 同理）。
    """
    os.environ["DB_NAME"] = args.db_name
    os.environ["REDIS_DB"] = str(args.redis_db)
    os.environ["APP_ENV"] = "dataset"
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _parse_end(value: str | None):
    """解析 ``--end``：ISO8601 → 带时区的 datetime，留空返回 None（用当前时刻）。

    朴素时间串（无时区）按 UTC 解释：生成器内部统一用 UTC 秒级时刻，
    这里若按本地时区猜，同一串参数在不同机器上会得到不同时间线。
    """
    if not value:
        return None
    from datetime import datetime, timezone

    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_env(args)

    from sqlalchemy import create_engine, func, select, text
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.db import bootstrap
    from app.db.migrate import upgrade_to_head
    from app.models.event import RcEvent
    from app.seeds import rules as rules_seed
    from app.simulator.dataset import DatasetSpec, generate

    if not settings.DB_NAME.endswith("dataset") and not args.reset:
        # 安全阀：目标库名不以 dataset 结尾时必须显式 --reset 才允许写入。
        # 这道闸门防的是"环境变量没生效 → 5 万条事件灌进开发库"这类不可逆事故。
        print(
            f"目标库 {settings.DB_NAME!r} 不是数据集库（应以 dataset 结尾）。"
            f"如确认要写入，请加 --reset。",
            file=sys.stderr,
        )
        return 2

    started = time.perf_counter()
    if args.reset:
        # 先确认没有自己持有的连接再 DROP：DROP DATABASE 会等所有活动连接释放
        # 元数据锁，留着连接会把自己锁死（表现为静默挂住）。
        bootstrap.drop_database(settings.DB_NAME)
        bootstrap.create_database_if_missing(settings.DB_NAME)
    else:
        bootstrap.create_database_if_missing(settings.DB_NAME)

    # 建表必须走 alembic 迁移，不能用 Base.metadata.create_all：
    # create_all 只会按模型建表，迁移里用 op.execute 写的"审计只增触发器"、
    # 索引与 alembic_version 都不会出现 —— 于是数据集库与开发库看起来表名一样，
    # 行为却不同（审计表可以被改删），这类差异在训练阶段完全暴露不出来。
    # 迁移是幂等的：新建库会从 base 一路升到 head，已建过的库等于空跑。
    try:
        upgrade_to_head(settings.db_url)
    except Exception as exc:  # noqa: BLE001 - 建表失败就必须立刻停：后面每个写入都依赖表结构
        # 最常见的失败：库是早期脚本用 Base.metadata.create_all 建的 ——
        # 表都在，但没有 alembic_version，迁移从起点开始补就会撞上"表已存在"。
        # 这种库只能重建（数据集是可再生的），因此这里直接给出下一步动作，
        # 而不是把一串 alembic 堆栈丢给使用者。
        print(
            f"迁移失败：{exc}\n"
            f"提示：若库 {settings.DB_NAME!r} 是早期用 create_all 建的"
            f"（没有 alembic_version 表），请加 --reset 重建一次。",
            file=sys.stderr,
        )
        return 2

    engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with session_factory() as db:
        # 种子规则：没有规则就没有规则分，模型也就学不到"规则看不见的那部分"。
        rules_seed.run(db)
        before = db.execute(select(func.count()).select_from(RcEvent)).scalar_one()
        if before and not args.keep_events:
            # 清空事件与决策：重复生成会与上一批的窗口数据叠加，
            # 同一个脚本两次跑出不同分布，"可复现"就不成立了。
            for table in (
                "rc_model_contribution",
                "rc_decision_hit",
                "rc_decision",
                "rc_feature_snapshot",
                "rc_event",
                "rc_audit_log",
                "biz_refund",
                "biz_order",
                "biz_coupon_receive",
                "biz_customer",
            ):
                db.execute(text(f"TRUNCATE TABLE {table}"))
            db.commit()
            print(f"已清空目标库中的历史数据（原有事件 {before} 条）")

        spec = DatasetSpec(
            events=args.limit,
            days=args.days,
            accounts=args.accounts,
            cheat_accounts=args.cheat_accounts,
            seed=args.seed,
            batch_size=args.batch_size,
            anchor=_parse_end(args.end),
        )
        report = generate(db, spec=spec, batch=args.batch)
        stored = db.execute(select(func.count()).select_from(RcEvent)).scalar_one()
        labeled = db.execute(
            select(func.count()).select_from(RcEvent).where(RcEvent.is_cheat.isnot(None))
        ).scalar_one()

    engine.dispose()

    print("\n===== 数据集生成结果 =====")
    print(report.summary())
    print(f"  库中事件 {stored} 条（其中已打标签 {labeled} 条）")
    print(
        f"  时间线 {report.spec.anchor:%Y-%m-%d %H:%M} ← {report.spec.days} 天"
        if report.spec.anchor
        else f"  时间线 右端=运行时刻（未指定 --end，跨时段重跑结果会漂移）"
    )
    print(
        f"  总耗时 {time.perf_counter() - started:.1f}s，"
        f"库 {settings.DB_NAME}，Redis DB {settings.REDIS_DB}"
    )

    issues = report.problems()
    if issues:
        print("\n⚠️ 分布未满足 PRD §14.1 口径：")
        for issue in issues:
            print(f"   - {issue}")
        return 1
    print("\n✅ 分布符合 PRD §14.1 口径")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
