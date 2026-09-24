"""审计哈希链测试：规范化稳定性、链完整性、篡改与删除检测。

重点覆盖的风险：
    1. 同一业务内容在不同调用路径下必须算出相同哈希（规范化做对了的标志）；
    2. 改一行内容必须被检出（且定位到该行）；
    3. 删除中间一条必须被检出（prev_hash 断链）；
    4. 数据库触发器必须挡住 UPDATE/DELETE（这是第一道防线）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text

from app.models.audit import GENESIS_HASH, RcAuditLog
from app.services import audit_service

# 迁移里创建的两个触发器 DDL。
# 测试中"绕过触发器演示篡改"后必须按原样重建，因此把 DDL 提取成常量，
# 避免测试与迁移脚本各写一份、日后改了一处忘了另一处。
UPDATE_TRIGGER_DDL = (
    "CREATE TRIGGER trg_rc_audit_log_no_update "
    "BEFORE UPDATE ON rc_audit_log FOR EACH ROW "
    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'rc_audit_log is append-only: UPDATE is forbidden'"
)
DELETE_TRIGGER_DDL = (
    "CREATE TRIGGER trg_rc_audit_log_no_delete "
    "BEFORE DELETE ON rc_audit_log FOR EACH ROW "
    "SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'rc_audit_log is append-only: DELETE is forbidden'"
)

pytestmark = pytest.mark.integration


def _run_ddl(db, engine, statements: list[str]) -> None:
    """在独立连接上执行 DDL（建/删触发器）。

    **必须先结束 db 会话的未提交事务**：MySQL 的 DDL 需要表的元数据锁（MDL），
    而一个"挂着未提交事务"的连接会一直持有 MDL，导致 CREATE/DROP TRIGGER
    无限等待（表现为测试卡死、无异常、无超时）。
    这个坑在真实演示脚本里同样存在，因此处置逻辑集中在这一个函数里。
    """
    db.commit()
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


@pytest.fixture(autouse=True)
def _clean_chain(engine, db):
    """每个测试前清空审计链。

    本模块有测试必须真正 commit（DDL 与跨连接可见性都需要），
    因此默认的"测试结束回滚"保护在这里不成立 —— 不主动清空的话，
    上一轮留下的记录会把下一轮的链校验污染成"第 N 条断裂"。
    用 TRUNCATE 而不是 DELETE：DELETE 会被只增触发器挡住，
    而 TRUNCATE 属于 DDL，不触发 DELETE 触发器。
    """
    db.rollback()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE rc_audit_log"))
    yield
    db.rollback()


def test_genesis_chain_for_first_record(db) -> None:
    row = audit_service.write(db, action="login", actor_id="1", actor_name="管理员", role="admin")
    assert row.prev_hash == GENESIS_HASH
    assert len(row.hash) == 64
    assert audit_service.verify(db).valid is True


def test_chain_links_sequential(db) -> None:
    first = audit_service.write(db, action="a1", actor_id="1")
    second = audit_service.write(db, action="a2", actor_id="1")
    third = audit_service.write(db, action="a3", actor_id="1")

    assert second.prev_hash == first.hash
    assert third.prev_hash == second.hash
    report = audit_service.verify(db)
    assert report.valid is True
    assert report.total == 3


def test_canonical_json_is_key_order_independent() -> None:
    """键顺序不同必须产出同一字符串（否则哈希会随代码写法漂移）。"""
    left = audit_service.canonical_json({"b": 1, "a": 2})
    right = audit_service.canonical_json({"a": 2, "b": 1})
    assert left == right == '{"a":2,"b":1}'


def test_canonical_json_keeps_chinese_readable() -> None:
    """中文不转义：审计的第一价值是人能读懂。"""
    text_value = audit_service.canonical_json({"reason": "批量注册"})
    assert "批量注册" in text_value
    assert "\\u" not in text_value


def test_canonical_json_normalizes_datetime_to_second() -> None:
    value = audit_service.canonical_json(
        {"t": datetime(2026, 9, 24, 1, 2, 3, 987654)}
    )
    assert value == '{"t":"2026-09-24T01:02:03"}'


def test_same_content_same_hash_across_calls(db) -> None:
    """同一业务内容 + 同一 prev，两次计算结果必须一致。"""
    fixed_time = datetime(2026, 9, 24, 3, 0, 0)
    row = RcAuditLog(
        actor_id="1", actor_name="x", role="admin", action="login",
        target_type="user", target_id="9", reason="r",
        created_at=fixed_time, prev_hash=GENESIS_HASH, hash="",
    )
    first = audit_service.compute_hash(GENESIS_HASH, row)
    second = audit_service.compute_hash(GENESIS_HASH, row)
    assert first == second


def test_before_after_serialized_as_json(db) -> None:
    row = audit_service.write(
        db,
        action="rule_update",
        actor_id="2",
        target_type="rule",
        target_id="RC_ENV_001",
        before={"score": 30},
        after={"score": 40},
        reason="阈值调整",
    )
    assert row.before_json == '{"score":30}'
    assert row.after_json == '{"score":40}'
    assert audit_service.verify(db).valid is True


def test_tampering_is_detected(db, engine) -> None:
    """绕过触发器直接改内容（模拟"有 DBA 权限的攻击者"）应被链校验发现。

    这里刻意先 DROP 触发器：这正是演示脚本要走的路径 ——
    第一层（触发器）挡住普通篡改，绕过第一层后第二层（哈希链）仍然发现。
    """
    audit_service.write(db, action="a1", actor_id="1")
    target = audit_service.write(db, action="a2", actor_id="1", reason="原始原因")
    audit_service.write(db, action="a3", actor_id="1")
    db.commit()
    target_id = target.id

    _run_ddl(db, engine, ["DROP TRIGGER IF EXISTS trg_rc_audit_log_no_update"])
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE rc_audit_log SET reason = :reason WHERE id = :id"),
            {"reason": "被篡改的原因", "id": target_id},
        )
    try:
        report = audit_service.verify(db)
        assert report.valid is False
        assert report.first_broken_id == target_id
        assert "哈希不匹配" in (report.detail or "")
    finally:
        _run_ddl(db, engine, [UPDATE_TRIGGER_DDL])


def test_deletion_is_detected(db, engine) -> None:
    """删除中间一条 -> prev_hash 断链，必须被检出。"""
    audit_service.write(db, action="a1", actor_id="1")
    middle = audit_service.write(db, action="a2", actor_id="1")
    audit_service.write(db, action="a3", actor_id="1")
    db.commit()
    middle_id = middle.id

    _run_ddl(db, engine, ["DROP TRIGGER IF EXISTS trg_rc_audit_log_no_delete"])
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM rc_audit_log WHERE id = :id"), {"id": middle_id})
    try:
        report = audit_service.verify(db)
        assert report.valid is False
        assert report.first_broken_id is not None
    finally:
        _run_ddl(db, engine, [DELETE_TRIGGER_DDL])


def test_triggers_block_update_and_delete(db, engine) -> None:
    """触发器是常驻的第一道防线：普通路径下 UPDATE/DELETE 必须直接报错。"""
    row = audit_service.write(db, action="a1", actor_id="1")
    db.commit()

    db.commit()
    with pytest.raises(Exception) as update_exc:
        with engine.begin() as conn:
            conn.execute(text("UPDATE rc_audit_log SET reason='x' WHERE id=:id"), {"id": row.id})
    assert "append-only" in str(update_exc.value)

    with pytest.raises(Exception) as delete_exc:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM rc_audit_log WHERE id=:id"), {"id": row.id})
    assert "append-only" in str(delete_exc.value)


def test_verify_reports_first_broken(db) -> None:
    """返回 first_broken_id 便于定位从哪一条开始不可信。"""
    for index in range(5):
        audit_service.write(db, action=f"a{index}", actor_id="1")
    report = audit_service.verify(db)
    assert report.valid is True
    assert report.first_broken_id is None
    assert report.total == 5
