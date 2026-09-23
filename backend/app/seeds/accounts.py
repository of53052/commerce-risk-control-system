"""sys_user 种子：三个角色账号（docs/PRD.md §4.3）。

口令来源说明：这是**演示项目**的预置账号，明文写在文档里是有意为之
（答辩/演示需要可复述的登录口令）。真实项目里这类口令必须由部署流程注入，
绝不能出现在版本库里。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.sys import ROLE_ADMIN, ROLE_AUDITOR, ROLE_STRATEGIST, STATUS_ENABLED, SysUser

NAME = "accounts"

# (用户名, 明文口令, 角色, 展示名)
ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    ("admin", "admin123", ROLE_ADMIN, "系统管理员"),
    ("strategist", "strategy123", ROLE_STRATEGIST, "风控策略师"),
    ("auditor", "audit123", ROLE_AUDITOR, "风控审核员"),
)


def run(db: Session) -> int:
    existing = {row.username: row for row in db.execute(select(SysUser)).scalars()}
    affected = 0

    for username, plain, role, real_name in ACCOUNTS:
        digest = hash_password(plain)
        row = existing.get(username)
        if row is None:
            db.add(
                SysUser(
                    username=username,
                    password_hash=digest,
                    real_name=real_name,
                    role=role,
                    status=STATUS_ENABLED,
                )
            )
            affected += 1
        else:
            # 重复执行时强制把口令/角色/状态刷回文档口径：
            # 演示前跑一次 init_db.ps1 就能保证"文档里的口令一定能登进去"。
            row.password_hash = digest
            row.role = role
            row.real_name = real_name
            row.status = STATUS_ENABLED
            affected += 1

    return affected

