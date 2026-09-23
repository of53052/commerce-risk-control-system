"""数据库引擎与会话。

要点：
1. pool_pre_ping：本地 MySQL 常被手动重启，连接可能已失效；开启预检可自动重连，
   避免第一次请求报 "MySQL server has gone away"。
2. pool_recycle=3600：小于 MySQL 默认 wait_timeout(8h)，防止空闲连接被服务端断开。
3. get_db 作为 FastAPI 依赖，保证请求级会话的创建与关闭。
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

engine = create_engine(
    settings.db_url,
    echo=settings.DB_ECHO,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖：请求结束自动关闭会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
