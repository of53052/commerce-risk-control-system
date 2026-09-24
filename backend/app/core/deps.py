"""FastAPI 依赖：会话、鉴权、角色校验。

两条通道（docs/ARCHITECTURE.md §10）在这里物理分离，不会互相污染：

* **运营侧 JWT**：``Authorization: Bearer <token>`` → :class:`Operator`；
* **业务侧 API Key**：``X-API-Key: <key>`` → :class:`BizCaller`。

为什么用两个独立的依赖而不是"任选其一"的单一依赖：两条通道的授权范围**不重叠** ——
拿 JWT 的人不应该能投递事件（那会绕过业务幂等键的归属约定），
拿 API Key 的机器也不应该能查案件、改规则。单一依赖会让"某天多给一个
``or`` 分支"变成一次静默的越权。

``Operator`` / ``BizCaller`` 都是不可变 dataclass：它们会被多个下游共享，
可变对象在依赖注入的缓存语义下很容易被某处意外改写（FastAPI 会缓存同一请求内的
依赖返回值，所有使用点拿到的是**同一个对象**）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import log_kv, trace_id_ctx
from app.core.security import InvalidTokenError, decode_access_token, hash_api_key
from app.core.timeutil import utcnow
from app.db.session import get_db
from app.models.sys import STATUS_ENABLED, SysApiKey, SysUser
from app.services.errors import AuthError, ErrorCode, PermissionError_

logger = logging.getLogger("app.core.deps")

# auto_error=False：凭据缺失时不抛 FastAPI 默认的 403，而是交给我们统一成 40101，
# 保证"没带 token"与"token 过期"在响应结构上一致（错误码不同、结构相同）。
_bearer = HTTPBearer(auto_error=False, scheme_name="运营侧 JWT")
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="业务侧 API Key")

# last_used_at 的写入节流窗口（秒）：见 require_biz_key 的注释。
_LAST_USED_THROTTLE_SECONDS = 60


@dataclass(frozen=True)
class Operator:
    """当前登录的后台用户。"""

    id: int
    username: str
    real_name: str | None
    role: str


@dataclass(frozen=True)
class BizCaller:
    """当前调用的业务方（由 API Key 标识）。"""

    key_id: int
    name: str


def get_current_operator(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Operator:
    """解析 JWT 并加载用户。任何失败都映射成 401 家族的明确错误码。"""
    if credentials is None or not credentials.credentials:
        raise AuthError("缺少访问令牌，请先登录", code=ErrorCode.AUTH_REQUIRED)

    try:
        payload = decode_access_token(credentials.credentials)
    except InvalidTokenError as exc:
        # jwt 库对"过期"与"签名错"给的是同一族异常，但用户需要看到不同的提示：
        # 过期要"重新登录"，签名错通常意味着配置漂移，提示口径必须分开。
        message = str(exc)
        code = ErrorCode.TOKEN_EXPIRED if "expired" in message.lower() else ErrorCode.TOKEN_INVALID
        raise AuthError("访问令牌无效或已过期，请重新登录", code=code, detail=message) from exc

    user_id = payload.get("sub")
    user = db.execute(select(SysUser).where(SysUser.id == int(user_id))).scalar_one_or_none() if user_id else None
    if user is None:
        raise AuthError("令牌对应的用户不存在", code=ErrorCode.TOKEN_INVALID)
    if user.status != STATUS_ENABLED:
        # 停用账号的令牌可能还没过期，必须在每次请求校验状态，
        # 否则"停用"要等到令牌自然过期才生效（最长 7 天）。
        raise AuthError("账号已被停用", code=ErrorCode.ACCOUNT_DISABLED)

    log_kv(logger, logging.DEBUG, "JWT 鉴权通过", username=user.username, role=user.role)
    return Operator(id=user.id, username=user.username, real_name=user.real_name, role=user.role)


def require_roles(*roles: str):
    """角色校验依赖工厂。用法：``operator: Operator = Depends(require_roles("admin"))``。

    权限矩阵见 docs/PRD.md §4.2；这里不做"admin 自动包含一切"的隐式推导 ——
    每个接口显式列出允许的角色，"admin 看到所有接口"这件事在代码里就是可见的。
    """
    allowed = frozenset(roles)

    def _guard(operator: Operator = Depends(get_current_operator)) -> Operator:
        if operator.role not in allowed:
            raise PermissionError_(
                f"当前角色 {operator.role} 无权访问该接口",
                code=ErrorCode.PERMISSION_DENIED,
                detail={"required": sorted(allowed), "current": operator.role},
            )
        return operator

    return _guard


def require_biz_key(
    api_key: str | None = Depends(_api_key_header),
    db: Session = Depends(get_db),
) -> BizCaller:
    """校验业务侧 API Key。

    查库方式：先算 SHA-256 摘要，再按 ``api_key_hash`` 唯一索引做**等值查找**。
    这正是"API Key 用 SHA-256 而非 bcrypt"的第二个收益（见 core/security.py）：
    确定性摘要让这一步是索引点查，而不是"取出全部 Key 逐个 bcrypt 比对"。

    ``last_used_at`` 的更新做了 60 秒节流：它是运维信息（识别僵尸密钥），
    但每次事件接入都写一次库会让这个字段变成热行，在高并发下形成锁竞争。
    60 秒的精度对"这把 Key 最近还在用吗"完全够用。
    """
    if not api_key:
        raise AuthError(
            "缺少 X-API-Key 请求头",
            code=ErrorCode.API_KEY_MISSING,
            field="X-API-Key",
        )

    digest = hash_api_key(api_key)
    row = db.execute(select(SysApiKey).where(SysApiKey.api_key_hash == digest)).scalar_one_or_none()
    if row is None:
        log_kv(logger, logging.WARNING, "API Key 校验失败", trace_id=trace_id_ctx.get())
        raise AuthError("API Key 无效", code=ErrorCode.API_KEY_INVALID, field="X-API-Key")
    if not row.enabled:
        raise AuthError("API Key 已被停用", code=ErrorCode.API_KEY_INVALID, field="X-API-Key")

    _touch_last_used(db, row)
    return BizCaller(key_id=row.id, name=row.name)


def _touch_last_used(db: Session, row: SysApiKey) -> None:
    """节流更新 last_used_at（见 require_biz_key 的注释）。"""
    now = utcnow()
    if row.last_used_at is not None and (now - row.last_used_at).total_seconds() < _LAST_USED_THROTTLE_SECONDS:
        return
    row.last_used_at = now
    db.commit()


def current_operator_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    """尽力解析用户名，失败返回 None（用于审计留痕，不做拦截）。"""
    if credentials is None:
        return None
    try:
        return str(decode_access_token(credentials.credentials).get("username"))
    except InvalidTokenError:
        return None
