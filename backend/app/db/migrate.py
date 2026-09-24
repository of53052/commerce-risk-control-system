"""迁移入口（供脚本调用，不依赖命令行）。

**为什么必须有这个模块**：建表只能有一条路径 —— alembic 迁移。脚本若改用
``Base.metadata.create_all``，建出的表会缺迁移里用 ``op.execute`` 写的对象
（如 ``rc_audit_log`` 的"只增"触发器）与 ``alembic_version``，于是"数据集库"
与"开发库"表名相同、行为不同；这类差异不会在训练阶段暴露，只在演示写审计日志
或回滚时才炸出来（评审发现的历史缺陷）。

用法：``upgrade_to_head(db_url)``。连接串由调用方传入，本模块**不读 settings**，
避免"脚本以为在写数据集库、迁移却升了开发库"的错位。
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger("app.db.migrate")

#: 仓库内的 alembic 脚本目录（backend/alembic）。
ALEMBIC_SCRIPT_LOCATION = Path(__file__).resolve().parents[2] / "alembic"


def upgrade_to_head(db_url: str) -> None:
    """把 ``db_url`` 指向的库升级到迁移头（幂等：已是最新则空跑）。

    连接串通过 ``config.cmd_opts.x`` 传给 ``alembic/env.py``，与命令行
    ``alembic -x db_url=... upgrade head`` 完全等价（env.py 的 ``get_url()``
    优先读这个值）。这样口令不必写进 ``alembic.ini``，也不依赖"进程环境里
    settings 指向哪个库"这一隐式前提。

    刻意**不加载 alembic.ini**：它的 ``[loggers]`` 段会把根 logger 重置成
    WARNING，调用方（如数据集生成脚本）的进度日志会被静默吞掉。
    """
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_LOCATION))
    config.cmd_opts = SimpleNamespace(x=[f"db_url={db_url}"])

    logger.info("执行迁移 upgrade head")
    command.upgrade(config, "head")


if __name__ == "__main__":  # pragma: no cover - 便捷入口（防止被当作命令行工具误用）
    raise SystemExit(
        "迁移请用 `python -m alembic upgrade head`，或由脚本调用 "
        "app.db.migrate.upgrade_to_head(db_url)"
    )