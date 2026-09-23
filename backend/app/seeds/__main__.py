"""种子执行入口：``python -m app.seeds``（由 scripts/init_db.ps1 调用）。"""

from __future__ import annotations

import logging

from app.core.logging import setup_logging
from app.db.session import SessionLocal
from app.seeds import run_all


def main() -> int:
    setup_logging()
    logging.getLogger("app.seeds").info("开始写入种子数据")
    with SessionLocal() as db:
        results = run_all(db)
    for item in results:
        print(f"  - 种子 {item.name}: 影响 {item.affected} 行")
    print("种子数据写入完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

