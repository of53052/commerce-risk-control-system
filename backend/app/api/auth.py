"""认证接口：登录、当前用户。

登录成功会写一条审计记录（``action=login``）：PRD §10.1 要求「登录登出」入审计。
审计与登录查询放在同一事务里提交，保证"登录成功但审计没记上"的窗口不存在。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.response import success
from app.core.deps import Operator, get_current_operator
from app.core.logging import log_kv
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.models.sys import STATUS_ENABLED, SysUser
from app.schemas.auth import LoginIn, TokenOut, UserOut
from app.services import audit_service
from app.services.errors import AuthError, ErrorCode

logger = logging.getLogger("app.api.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@router.post("/login", summary="登录换取 JWT")
def login(payload: LoginIn, db: Session = Depends(get_db)) -> dict:
    """账号口令登录。

    **失败原因不外露**：账号不存在与口令错误返回同一个错误码与文案。
    区分二者会让攻击者能枚举有效账号；对合法用户来说也没有信息增益
    （两种情况要做的事都是"检查账号和口令"）。
    """
    user = db.execute(select(SysUser).where(SysUser.username == payload.username)).scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        log_kv(logger, logging.WARNING, "登录失败", username=payload.username)
        raise AuthError("账号或口令不正确", code=ErrorCode.CREDENTIAL_INVALID)
    if user.status != STATUS_ENABLED:
        raise AuthError("账号已被停用，请联系管理员", code=ErrorCode.ACCOUNT_DISABLED)

    token, expires_at = create_access_token(user.id, user.username, user.role)
    audit_service.write(
        db,
        action="login",
        actor_id=str(user.id),
        actor_name=user.username,
        role=user.role,
        target_type="user",
        target_id=str(user.id),
        reason="登录成功",
        commit=True,
    )
    log_kv(logger, logging.INFO, "登录成功", username=user.username, role=user.role)
    return success(
        TokenOut(
            access_token=token,
            expires_at=expires_at,
            user=UserOut(
                id=user.id,
                username=user.username,
                real_name=user.real_name,
                role=user.role,
                status=user.status,
            ),
        ).model_dump(mode="json")
    )


@router.get("/me", summary="当前登录用户")
def me(operator: Operator = Depends(get_current_operator)) -> dict:
    return success(
        UserOut(
            id=operator.id,
            username=operator.username,
            real_name=operator.real_name,
            role=operator.role,
            status=STATUS_ENABLED,
        ).model_dump(mode="json")
    )
