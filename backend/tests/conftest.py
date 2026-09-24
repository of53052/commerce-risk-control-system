"""pytest 夹具与测试环境隔离。

**隔离策略（这是本文件最重要的部分）**：

1. **环境变量必须在导入 app 之前设置**。``app.core.config.settings`` 是模块级单例，
   一旦被 import 就已经读完了 ``.env``。因此这里在文件顶部（所有 app 导入之前）
   就把 ``DB_NAME`` 指向 ``risk_control_test``、``REDIS_DB`` 指向 1。
   pydantic-settings 的优先级是「环境变量 > .env 文件」，所以这样能可靠覆盖。
2. **专用测试库 ``risk_control_test``**：每个会话开始时重建（drop + create），
   绝不触碰开发库 ``risk_control``。
3. **Redis DB=1**：开发用 DB=0，测试用 DB=1，测试开始前 FLUSHDB。
4. **建表用 ``Base.metadata.create_all`` 而非 alembic upgrade**：
   测试关心的是"模型与业务逻辑"，迁移正确性由 ``alembic check`` 与
   ``scripts/init_db.ps1`` 单独把关（两者在 CI/本地验收里都会跑）。
   用 create_all 省掉每个测试会话跑一遍迁移的开销。
5. **一个兜底断言**：``_assert_test_environment`` 会校验库名与 Redis DB，
   万一将来有人改坏了环境变量，测试会立刻失败而不是把开发数据洗掉。
"""

from __future__ import annotations

import os

# ---- 1. 环境隔离（必须早于任何 app 导入）----
os.environ["APP_ENV"] = "test"
os.environ["DB_NAME"] = "risk_control_test"
os.environ["REDIS_DB"] = "1"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import bootstrap  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.redis_client import get_redis, reset_cache  # noqa: E402
import app.models  # noqa: E402,F401 - 触发全部模型注册


def _assert_test_environment() -> None:
    """安全阀：确认当前指向的是测试库与测试 Redis DB。"""
    assert settings.DB_NAME.endswith("_test"), f"测试库名异常：{settings.DB_NAME}"
    assert settings.REDIS_DB == 1, f"测试 Redis DB 异常：{settings.REDIS_DB}"


@pytest.fixture(scope="session")
def engine():
    """会话级引擎：整个测试会话共用，开始时重建测试库与表结构。"""
    _assert_test_environment()

    # 先连实例层删库重建，保证"上一轮跑坏的残留 schema"不会污染本轮
    bootstrap.drop_database(settings.DB_NAME)
    bootstrap.create_database_if_missing(settings.DB_NAME)

    from sqlalchemy import create_engine

    eng = create_engine(settings.db_url, pool_pre_ping=True, future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine) -> Session:
    """函数级会话：每个测试一个事务，结束后回滚，测试之间互不影响。"""
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
        session.rollback()
    finally:
        session.close()


@pytest.fixture()
def redis_client(engine):
    """函数级 Redis（DB=1），每个测试前清空，避免条带残留串味。"""
    _assert_test_environment()
    reset_cache()
    client = get_redis()
    client.flushdb()
    yield client
    client.flushdb()


@pytest.fixture()
def real_db(engine) -> Session:
    """会真正提交的会话。

    少数测试需要跨会话可见（例如 event_gateway 落库后由另一路径读取），
    默认的 ``db`` 夹具是回滚语义，满足不了这种场景。
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()

