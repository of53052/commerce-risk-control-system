"""sys_api_key 种子：业务端调用凭证。

明文来自 ``.env`` 的 ``API_KEY_SEED``，库里只存 SHA-256 摘要
（改算法的原因见 app/core/security.py 顶部注释）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_api_key
from app.models.sys import SysApiKey

NAME = "api_keys"

KEY_NAME = "模拟业务端"


def run(db: Session) -> int:
    raw = settings.API_KEY_SEED
    if not raw:
        raise ValueError("API_KEY_SEED 为空，请检查 backend/.env")

    digest = hash_api_key(raw)
    row = db.execute(
        select(SysApiKey).where(SysApiKey.api_key_hash == digest)
    ).scalar_one_or_none()

    if row is None:
        db.add(SysApiKey(name=KEY_NAME, api_key_hash=digest, enabled=True))
        return 1

    # 已存在则确保启用（重复执行幂等；也避免误禁用后忘记恢复）
    if not row.enabled or row.name != KEY_NAME:
        row.enabled = True
        row.name = KEY_NAME
        return 1
    return 0

