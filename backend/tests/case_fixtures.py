"""案件相关测试的共享夹具与数据构造器（被三个 test_case*.py 导入）。

放在独立模块而不是 ``conftest.py`` 的理由：这些夹具**只服务案件测试**，
塞进全局 conftest 会让事件/特征/规则的测试也背上"清空案件表"的成本与
理解负担；而 pytest 允许测试模块 ``from tests.case_fixtures import ...``
把夹具函数导入自己的命名空间，效果与写在文件里完全一致。

**清表用 TRUNCATE 而非 DELETE**：``rc_audit_log`` 上的"只增"触发器会挡住
DELETE（BEFORE DELETE 直接 SIGNAL），TRUNCATE 是 DDL 不触发 DML 触发器。
执行前必须先把未提交事务回滚，否则 DDL 与未提交 DML 争抢表级元数据锁会
**静默挂死**（无异常、无输出，只能靠 pytest-timeout 兜住）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.security import create_access_token
from app.core.timeutil import utcnow
from app.db.session import get_db
from app.models.event import RcEvent
from app.models.sys import ROLE_ADMIN, ROLE_AUDITOR, ROLE_STRATEGIST, STATUS_ENABLED, SysUser
from app.services import case_service

# 每个测试前清空的表。案件四表放在最前：它们逻辑上引用事件与决策，
# 写成"从下到上"的顺序便于人工核对是否漏表（本系统不建物理外键，
# 因此顺序不影响正确性，只影响可读性）。
TRUNCATE_TABLES = (
    "rc_case",
    "rc_case_event",
    "rc_case_action",
    "rc_case_action_item",
    "rc_event",
    "rc_feature_snapshot",
    "rc_decision",
    "rc_decision_hit",
    "rc_model_contribution",
    "rc_audit_log",
    "rc_list_entry",
    "sys_config",
    "sys_user",
    "biz_order",
    "biz_refund",
    "biz_customer",
    "biz_coupon_receive",
)


def truncate_all(db: Session) -> None:
    """清空业务表（见表清单上方的说明）。"""
    db.rollback()
    for name in TRUNCATE_TABLES:
        db.execute(text(f"TRUNCATE TABLE {name}"))
    db.commit()


@pytest.fixture
def case_db(engine, redis_client) -> Session:
    """会真正提交的会话（案件服务与处置服务内部自己 commit）。

    不能复用 conftest 里"每例回滚"的 ``db`` 夹具：状态机与处置联动的语义
    就是"提交后可见"，回滚夹具会把要验证的行为一起回滚掉。
    """
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    truncate_all(session)
    from app.services import config_service, list_service, rule_engine

    config_service.invalidate_cache()
    rule_engine.invalidate_cache()
    list_service.invalidate_cache()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def make_user(db: Session, username: str, role: str) -> SysUser:
    """建一个可登录用户（口令哈希用占位串：本文件不测登录）。"""
    user = SysUser(
        username=username,
        password_hash="x",
        real_name=username,
        role=role,
        status=STATUS_ENABLED,
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def auditor(case_db: Session) -> SysUser:
    return make_user(case_db, "auditor1", ROLE_AUDITOR)


@pytest.fixture
def other_auditor(case_db: Session) -> SysUser:
    return make_user(case_db, "auditor2", ROLE_AUDITOR)


@pytest.fixture
def admin(case_db: Session) -> SysUser:
    return make_user(case_db, "admin1", ROLE_ADMIN)


@pytest.fixture
def strategist(case_db: Session) -> SysUser:
    return make_user(case_db, "strat1", ROLE_STRATEGIST)


@pytest.fixture
def client(case_db: Session) -> TestClient:
    """接口测试客户端：把 ``get_db`` 依赖换成测试会话，保证两边看到同一份数据。"""
    from app.main import app

    app.dependency_overrides[get_db] = lambda: case_db
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def auth_headers(user: SysUser) -> dict[str, str]:
    """直接签发 JWT（不经过登录接口：登录链路由 test_event_gateway 覆盖）。"""
    token, _ = create_access_token(user.id, user.username, user.role)
    return {"Authorization": f"Bearer {token}"}


def actor_of(user: SysUser) -> case_service.Actor:
    """SysUser -> 服务层 Actor。"""
    return case_service.Actor(id=user.id, name=user.username, role=user.role)


def make_case(
    db: Session,
    *,
    user_id: str = "U1",
    scene: str = "coupon",
    event_id: str = "EVT-1",
    decision_id: str = "D-1",
    event_type: str = "coupon_receive",
    action: str = "Reject",
    risk_level: str = "high",
    risk_score: int = 85,
    hit_count: int = 2,
    biz_no: str | None = None,
    occurred_at=None,
    commit: bool = True,
):
    """直接调服务建案/合案（网关链路由 test_event_gateway 覆盖）。"""
    result = case_service.merge_or_create(
        db,
        subject_value=user_id,
        scene=scene,
        event_id=event_id,
        decision_id=decision_id,
        event_type=event_type,
        action=action,
        risk_level=risk_level,
        risk_score=risk_score,
        hit_count=hit_count,
        biz_no=biz_no,
        occurred_at=occurred_at or utcnow(),
    )
    if commit:
        db.commit()
    return result


def make_event_row(
    db: Session,
    *,
    event_id: str = "EVT-1",
    user_id: str = "U1",
    event_type: str = "coupon_receive",
    device_id: str | None = "DEV-1",
    ip: str = "10.1.1.1",
    phone: str = "13800000001",
    biz_no: str | None = None,
    occurred_at=None,
) -> RcEvent:
    """落一条 rc_event（详情页、图谱与"封设备"联动都从它取数）。"""
    row = RcEvent(
        event_id=event_id,
        event_type=event_type,
        user_id=user_id,
        phone=phone,
        device_id=device_id,
        ip=ip,
        biz_no=biz_no,
        occurred_at=occurred_at or utcnow(),
        payload={},
    )
    db.add(row)
    db.commit()
    return row


def count(db: Session, model) -> int:
    """表行数（测试断言常用）。"""
    return int(db.execute(select(func.count()).select_from(model)).scalar_one())


def audit_actions(db: Session) -> list[str]:
    """审计里的动作清单（按写入顺序）。"""
    return list(db.execute(text("SELECT action FROM rc_audit_log ORDER BY id")).scalars().all())
