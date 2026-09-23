"""audit log append-only triggers

Revision ID: 3f7122a28be3
Revises: e62c7b4dd2ad
Create Date: 2026-09-23 20:44:49.511488

作用：给 `rc_audit_log` 加 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，
命中即 `SIGNAL SQLSTATE '45000'` 直接报错，使审计表在数据库层面成为「只增表」。

为什么用触发器而不是只靠应用层自律（docs/ARCHITECTURE.md 7.7）：
    应用层「不写 UPDATE/DELETE」只是约定，任何一个绕过 ORM 的脚本、一次手工 SQL
    都能篡改历史。触发器是最后一道兜底，且它对所有数据库账号一视同仁 —— 即使有人
    拿到了 root 也改不了（除非显式 DROP TRIGGER）。

与「演示审计链断裂」的关系（重要）：
    因为触发器对所有账号生效，演示「改一条记录 → 校验接口发现断裂」时必须
    先 `DROP TRIGGER` 再改，然后重建触发器。这不是缺陷，而是可展示的纵深防御：
    第一层（触发器）挡住了直接篡改，绕过第一层后第二层（哈希链校验）仍能发现。

注意事项：
    1) MySQL 不支持「禁用触发器」（MariaDB 有 DISABLE TRIGGER，MySQL 没有），
       因此 downgrade 只能 DROP。
    2) 触发器名在库内唯一；重建库时随表一起消失，属于预期行为。
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '3f7122a28be3'
down_revision: Union[str, None] = 'e62c7b4dd2ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 触发器体内不写 BEGIN...END：单条 SIGNAL 语句即可，避免 DELIMITER 相关的心智负担。
    op.execute(
        "CREATE TRIGGER trg_rc_audit_log_no_update "
        "BEFORE UPDATE ON rc_audit_log FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' "
        "SET MESSAGE_TEXT = 'rc_audit_log is append-only: UPDATE is forbidden'"
    )
    op.execute(
        "CREATE TRIGGER trg_rc_audit_log_no_delete "
        "BEFORE DELETE ON rc_audit_log FOR EACH ROW "
        "SIGNAL SQLSTATE '45000' "
        "SET MESSAGE_TEXT = 'rc_audit_log is append-only: DELETE is forbidden'"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_rc_audit_log_no_update")
    op.execute("DROP TRIGGER IF EXISTS trg_rc_audit_log_no_delete")
