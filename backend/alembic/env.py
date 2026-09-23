"""Alembic 运行环境。

核心职责：
1. 把项目根目录（backend/）加入 ``sys.path``，使 ``import app.*`` 在 alembic 命令下可用；
2. 从 ``app.core.config.settings`` 取连接串（口令不落 alembic.ini）；
3. 导入 ``app.models`` 采集全部表元数据，供 autogenerate / 手工迁移引用 ``target_metadata``；
4. 提供 ``run_migrations_offline()`` 与 ``run_migrations_online()`` 两种模式。

坑点备忘：
- 必须 ``import app.models`` 而非只 import app.db.base，否则 autogenerate 会认为
  「元数据是空的」并生成一堆 drop_table —— 这是 Alembic 最常见的踩坑点。
- ``compare_type=True`` 让字段类型变更也能被 autogenerate 检出（MySQL 上默认不比对类型）。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---- 1. 路径修正：backend/ 加入 sys.path（本文件位于 backend/alembic/env.py）----
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402 - 必须在 sys.path 修正之后导入
from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401 - 仅为触发模型注册（副作用导入）

# Alembic Config 对象
config = context.config

# 日志配置：alembic.ini 中的 logger 段落生效
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 目标元数据：迁移脚本的 autogenerate 依据
target_metadata = Base.metadata


def get_url() -> str:
    """返回迁移用连接串。

    优先取命令行 ``-x db_url=...``（CI/临时指向测试库时使用），
    否则取应用配置。避免把口令写进 alembic.ini 后被提交。
    """
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    return settings.db_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库（``alembic upgrade head --sql``）。"""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = get_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # 表级/列级注释变更也要比对，否则注释漂移发现不了
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

