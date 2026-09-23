"""SQLAlchemy 声明式基类。

要点：
1. 统一命名约定（naming_convention），让 Alembic 自动生成的约束名稳定可读；
   否则同一约束在不同环境下可能被命名为不同名字，导致迁移漂移。
2. 所有表显式指定 InnoDB + utf8mb4，避免依赖实例默认字符集。
3. 主键统一 BigInteger 自增；时间统一 DateTime（UTC naive），与 PRD §16 的
   「UTC 存储、Asia/Shanghai 展示」一致。
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # 表级默认参数：MySQL 存储引擎与字符集（对 SQLite 等其他方言无副作用）
    __table_args__ = {
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_general_ci",
    }
