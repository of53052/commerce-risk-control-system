"""认证相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    # 口令上限 72 字节是 bcrypt 的硬约束（见 app/core/security.py），
    # 这里用字符数 64 兜住（UTF-8 下中文最多 3 字节/字，64 字仍可能超，
    # 所以 security 层还有一次字节级校验，两层都保留）
    password: str = Field(min_length=1, max_length=64)


class UserOut(BaseModel):
    id: int
    username: str
    real_name: str | None = None
    role: str
    status: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserOut

