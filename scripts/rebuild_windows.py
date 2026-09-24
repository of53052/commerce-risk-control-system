"""离线重建 Redis 窗口条带（docs/ARCHITECTURE.md §6.2、PRD §14 运维要求）。

用法（在仓库根目录执行）::

    backend/.venv/Scripts/python.exe scripts/rebuild_windows.py --confirm
    backend/.venv/Scripts/python.exe scripts/rebuild_windows.py --confirm --db-name risk_control_dataset --redis-db 2
    backend/.venv/Scripts/python.exe scripts/rebuild_windows.py --confirm --days 14 --dry-run

**为什么需要这个脚本**：特征全部来自 Redis 里的条带，而 Redis 是**可丢缓存**
（重启、`FLUSHDB`、TTL 过期、容器重建都会清空）。条带没了以后，事件与决策
都还在 MySQL 里（事实数据），但线上所有频次/聚簇特征都会变成 0 ——
表现为"风控突然什么都放行"，而且**不报错、不告警**，因为"窗口内没有事件"
与"窗口内确实没发生事件"在特征引擎看来完全一样。有了这个脚本，
从 MySQL 回放就能把缓存重建出来，不必等条带重新积累。

**重建口径必须与线上写入完全一致**：
    * 实体清单调用 ``event_gateway.entities_of``（user / phone / device / ip / address），
      与在线写入**同一个函数**，不是"照抄一份"；
    * 金额字段取自 ``event_gateway.AMOUNT_FIELD``；
    * 主体去重标识取自 ``feature_engine.distinct_subject_entities``；
    * 窗口档位取自 ``feature_engine.DEFAULT_WINDOWS``。
    **任何一项在这里另写一份，重建出来的特征就与实时决策的口径不同**，
    而这类偏差在页面上看不出来（数字照样有值，只是含义变了）。
    因此本脚本不复制这些常量，而是直接 import 上述模块 ——
    将来注册表加维度时，重建逻辑自动跟随。

**只回放窗口内的数据**：条带 TTL 最长 7 天 + 10 分钟，更早的事件重建了也立刻过期，
白白占用 Redis 内存。默认回放最近 7 天（``--days`` 可调）。

安全阀：必须显式传 ``--confirm`` 才动 Redis（先 ``FLUSHDB`` 再写入）——
误执行会清掉线上正在使用的条带，让风控在重建期间"瞎"一阵子。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# 与 gen_dataset.py 一致：先定位 backend/，再在任何 app 导入之前设置环境变量
# （app.core.config.settings 是模块级单例，导入即读值）。
BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线重建 Redis 窗口条带")
    parser.add_argument("--db-name", default=None, help="数据源库名（默认读环境变量 DB_NAME）")
    parser.add_argument("--redis-db", type=int, default=None, help="目标 Redis DB（默认读环境变量 REDIS_DB）")
    parser.add_argument("--days", type=int, default=7, help="回放最近多少天（默认 7，与最长窗口对齐）")
    parser.add_argument("--batch-size", type=int, default=1000, help="每批写入的事件数")
    parser.add_argument("--confirm", action="store_true", help="确认执行（会先清空目标 Redis DB）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入（可与 --confirm 同用）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not args.confirm:
        print(
            "拒绝执行：本脚本会先清空目标 Redis DB 再重建条带，"
            "请确认数据库与 Redis DB 后加 --confirm",
            file=sys.stderr,
        )
        return 2
    if args.db_name:
        os.environ["DB_NAME"] = args.db_name
    if args.redis_db is not None:
        os.environ["REDIS_DB"] = str(args.redis_db)
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    import json

    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.core.timeutil import to_ms, utcnow
    from app.models.event import RcEvent
    from app.services import event_gateway, feature_engine, window_store

    started = time.perf_counter()
    engine = create_engine(settings.db_url, pool_pre_ping=True, future=True)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    since = utcnow() - timedelta(days=args.days)
    windows = feature_engine.DEFAULT_WINDOWS
    subjects = feature_engine.distinct_subject_entities()

    with session_factory() as db:
        total = db.execute(
            select(func.count(RcEvent.id)).where(RcEvent.occurred_at >= since)
        ).scalar_one()
        print(
            f"数据源：{settings.DB_NAME}｜目标 Redis DB {settings.REDIS_DB}｜"
            f"回放 {args.days} 天内事件 {total} 条"
        )
        if args.dry_run:
            print("dry-run：仅统计，未写入 Redis")
            engine.dispose()
            return 0

        redis = window_store.get_redis()
        redis.flushdb()
        print("目标 Redis DB 已清空，开始重建……")

        written = 0
        last_id = 0
        while True:
            rows = db.execute(
                select(RcEvent)
                .where(RcEvent.occurred_at >= since, RcEvent.id > last_id)
                .order_by(RcEvent.id)
                .limit(args.batch_size)
            ).scalars().all()
            if not rows:
                break
            for event in rows:
                last_id = event.id
                payload = event.payload
                if isinstance(payload, (str, bytes)):
                    payload = json.loads(payload)
                payload = payload or {}
                entities = event_gateway.entities_of(
                    user_id=event.user_id,
                    phone=event.phone,
                    device_id=event.device_id,
                    ip=event.ip,
                    address_hash=event.address_hash,
                )
                amount_field = event_gateway.AMOUNT_FIELD.get(event.event_type)
                amount = 0.0
                if amount_field:
                    try:
                        amount = float(payload.get(amount_field) or 0.0)
                    except (TypeError, ValueError):
                        amount = 0.0
                window_store.add_event_to_entities(
                    entities=entities,
                    event_type=event.event_type,
                    windows=windows,
                    ts_ms=to_ms(event.occurred_at),
                    event_id=event.event_id,
                    amount=amount,
                    entity_subjects={
                        entity: f"{prefix}:{event.user_id}"
                        for entity, prefix in subjects.items()
                    },
                )
                written += 1
            print(f"  已重建 {written}/{total}", end="\r")

        keys = redis.dbsize()
        print(f"\n重建完成：事件 {written} 条 → Redis 键 {keys} 个，耗时 {time.perf_counter() - started:.1f}s")
        print(
            "提示：Redis 键数应显著大于事件数（每个事件写 5 个实体 × 多个事件类型 × "
            "多个窗口），若两者量级相同，说明实体或窗口清单没生效。"
        )
    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
