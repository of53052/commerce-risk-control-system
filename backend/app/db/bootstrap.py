"""数据库初始化引导（建库）。

为什么单独一个模块：
1. ``CREATE DATABASE`` 必须连到「实例层级」而不是「库层级」，而 SQLAlchemy 的
   ``engine`` 是绑定到具体库的（``settings.db_url``），两者不是一回事；
2. 这个动作既要被 ``scripts/init_db.ps1``（人工初始化）调用，也要被
   ``tests`` 的集成测试夹具调用（每次在 ``risk_control_test`` 上重建 schema），
   放在模块里比在脚本里内联一段 SQL 更可控、可测。

幂等性：全部使用 ``IF NOT EXISTS``，重复执行不报错（docs/PRD.md §17.1 要求
``init_db.ps1`` 可重复执行）。
"""

from __future__ import annotations

import logging

import pymysql

from app.core.config import settings

logger = logging.getLogger("app.db.bootstrap")


def create_database_if_missing(
    db_name: str | None = None,
    charset: str = "utf8mb4",
    collate: str = "utf8mb4_general_ci",
) -> str:
    """确保目标库存在，返回最终使用的库名。

    注意 ``collate`` 必须与 ``app/db/base.py`` 中模型声明的
    ``mysql_collate`` 一致，否则「迁移建的表」与「手工建的表」排序规则会不一致，
    表现为字符串比较大小写敏感性差异 —— 这类问题在联表查询时才暴露，很难排。

    使用 PyMySQL 直连而不是 SQLAlchemy engine：这里不需要 ORM 与连接池，
    且 SQLAlchemy 的连接串强制带库名，反而绕不开「库不存在就报 1049」的死结。
    """
    target = db_name or settings.DB_NAME

    # 库名会拼进 SQL（DDL 无法参数化），因此必须做白名单校验，防止注入。
    if not target.replace("_", "").isalnum():
        raise ValueError(f"非法库名：{target!r}（只允许字母、数字、下划线）")

    conn = pymysql.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        charset=charset,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{target}` "
                f"CHARACTER SET {charset} COLLATE {collate}"
            )
        conn.commit()
    finally:
        conn.close()

    logger.info("数据库已就绪：%s", target)
    return target


def drop_database(db_name: str) -> None:
    """删除整个库 —— 仅供测试夹具与本地重置使用。

    刻意不做成 CLI 子命令：删库是破坏性动作，必须由调用方显式写代码触发，
    不能让它在命令行上被随手敲出来（AGENTS.md §10 破坏性操作需显式确认）。
    """
    if not db_name.replace("_", "").isalnum():
        raise ValueError(f"非法库名：{db_name!r}")
    # 生产环境硬保护：APP_ENV=prod 时一律拒绝，无论库名叫什么。
    # 开发/测试环境允许删库重建 —— 否则"迁移改动后从零验证"就无从做起，
    # 而"数据回滚 = 重建独立库"本身就是 docs/ARCHITECTURE.md §13 约定的回滚手段。
    if settings.APP_ENV == "prod":
        raise RuntimeError("APP_ENV=prod，拒绝执行删库操作")

    conn = pymysql.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        conn.commit()
    finally:
        conn.close()
    logger.warning("数据库已删除：%s", db_name)


if __name__ == "__main__":  # pragma: no cover - 便捷入口：python -m app.db.bootstrap
    logging.basicConfig(level=logging.INFO)
    create_database_if_missing()
