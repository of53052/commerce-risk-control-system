"""案件接口层测试：鉴权、分页外壳、错误码、权限矩阵与端到端闭环。

为什么服务层已经测过还要测接口：路由层承载三类只有走 HTTP 才能验证的契约 ——

1. **鉴权与权限**：缺 token 401、角色不够 403（依赖注入的写法错了服务层测不出来）；
2. **错误码 → HTTP 状态**的映射（409/403/404 三档，由全局异常处理器统一转换）；
3. **响应外壳**：``{code, message, data, trace_id}`` 与分页结构，
   前端直接按它写解析逻辑，字段名漂了就是联调事故。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.biz import ORDER_CANCELLED, ORDER_CREATED, BizOrder
from app.models.case import CASE_ARCHIVED, CASE_DISPOSED, CASE_PENDING, CASE_PROCESSING, RcCase
from app.models.sys import SysUser
from app.services.errors import ErrorCode
from tests.case_fixtures import (  # noqa: F401 - 夹具需导入进模块命名空间才能被 pytest 发现
    actor_of,
    admin,
    auditor,
    auth_headers,
    case_db,
    client,
    make_case,
    make_event_row,
    other_auditor,
    strategist,
)

pytestmark = pytest.mark.integration

_REMARK = "证据链完整，确认存在团伙化作弊行为"


def test_api_requires_login(client: TestClient) -> None:
    """未登录访问案件接口 -> 401（错误码是 401 家族）。"""
    response = client.get("/api/v1/cases")
    assert response.status_code == 401
    assert response.json()["code"] == ErrorCode.AUTH_REQUIRED


def test_api_list_and_detail(client: TestClient, case_db, auditor: SysUser) -> None:
    """列表返回分页体与状态计数；详情返回完整证据块；不存在的案件 404。"""
    result = make_case(case_db, event_id="EVT-1")
    headers = auth_headers(auditor)

    body = client.get("/api/v1/cases", headers=headers).json()
    assert body["code"] == "0"
    assert body["trace_id"]
    assert body["data"]["total"] == 1
    assert body["data"]["page"] == 1
    assert body["data"]["items"][0]["case_no"] == result.case_no
    assert body["data"]["items"][0]["status"] == CASE_PENDING
    assert body["data"]["status_counts"][CASE_PENDING] == 1

    detail = client.get(f"/api/v1/cases/{result.case_no}", headers=headers).json()
    assert detail["data"]["case"]["case_no"] == result.case_no
    assert "graph" in detail["data"]
    assert "profile" in detail["data"]

    missing = client.get("/api/v1/cases/C-NOPE", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["code"] == ErrorCode.CASE_NOT_FOUND


def test_api_list_filters_mine_and_keyword(
    client: TestClient, case_db, auditor: SysUser, other_auditor: SysUser
) -> None:
    """列表支持 mine= 与 keyword=（工作台左栏的筛选控件）。"""
    mine = make_case(case_db, user_id="U1", event_id="EVT-1")
    make_case(case_db, user_id="U2", event_id="EVT-2", decision_id="D-2")
    client.post(f"/api/v1/cases/{mine.case_no}/claim", headers=auth_headers(auditor))

    body = client.get("/api/v1/cases?mine=true", headers=auth_headers(other_auditor)).json()
    assert body["data"]["total"] == 0

    body = client.get("/api/v1/cases?mine=true", headers=auth_headers(auditor)).json()
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["case_no"] == mine.case_no

    body = client.get("/api/v1/cases?keyword=U2", headers=auth_headers(auditor)).json()
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["subject_value"] == "U2"


def test_api_full_disposal_flow(client: TestClient, case_db, auditor: SysUser, admin: SysUser) -> None:
    """接口闭环：接手 -> 处置（联动生效）-> 管理员归档。"""
    make_event_row(case_db, event_id="EVT-1", event_type="order_create", biz_no="ORD-7")
    case_db.add(
        BizOrder(
            order_no="ORD-7",
            user_id="U1",
            product_id="P1",
            quantity=1,
            amount=88.0,
            status=ORDER_CREATED,
        )
    )
    case_db.commit()
    result = make_case(case_db, scene="order", event_type="order_create", biz_no="ORD-7")
    headers = auth_headers(auditor)

    claimed = client.post(f"/api/v1/cases/{result.case_no}/claim", headers=headers)
    assert claimed.status_code == 200
    assert claimed.json()["data"]["status"] == CASE_PROCESSING
    assert claimed.json()["data"]["handler"] == "auditor1"

    disposed = client.post(
        f"/api/v1/cases/{result.case_no}/dispose",
        headers=headers,
        json={
            "business_result": "reject",
            "risk_actions": ["block_order"],
            "remark": _REMARK,
        },
    )
    assert disposed.status_code == 200
    payload = disposed.json()["data"]
    assert payload["case"]["status"] == CASE_DISPOSED
    assert payload["case"]["dispose_result"] == "reject"
    assert payload["items"][0]["exec_result"] == "success"
    order = case_db.execute(select(BizOrder)).scalar_one()
    assert order.status == ORDER_CANCELLED

    archived = client.post(
        "/api/v1/cases/archive",
        headers=auth_headers(admin),
        json={"case_nos": [result.case_no]},
    )
    assert archived.status_code == 200
    assert archived.json()["data"]["ok"] == 1
    assert case_db.execute(select(RcCase)).scalar_one().status == CASE_ARCHIVED


def test_api_claim_conflict_returns_409(
    client: TestClient, case_db, auditor: SysUser, other_auditor: SysUser
) -> None:
    """并发接手的第二种结果：409 + 当前处理人（前端据此提示"已被 XXX 接手"）。"""
    result = make_case(case_db, event_id="EVT-1")
    assert (
        client.post(f"/api/v1/cases/{result.case_no}/claim", headers=auth_headers(auditor)).status_code
        == 200
    )

    conflict = client.post(
        f"/api/v1/cases/{result.case_no}/claim", headers=auth_headers(other_auditor)
    )
    assert conflict.status_code == 409
    body = conflict.json()
    assert body["code"] == ErrorCode.STATE_CONFLICT
    assert body["detail"]["handler"] == "auditor1"
    assert body["data"] is None


def test_api_dispose_validation_error_returns_422(
    client: TestClient, case_db, auditor: SysUser
) -> None:
    """备注过短在 schema 层就被拦下（422），服务层校验作为第二道防线。"""
    result = make_case(case_db, event_id="EVT-1")
    client.post(f"/api/v1/cases/{result.case_no}/claim", headers=auth_headers(auditor))

    response = client.post(
        f"/api/v1/cases/{result.case_no}/dispose",
        headers=auth_headers(auditor),
        json={"business_result": "reject", "risk_actions": ["pass"], "remark": "太短"},
    )
    assert response.status_code == 422


def test_api_strategist_can_read_but_not_dispose(
    client: TestClient, case_db, strategist: SysUser
) -> None:
    """strategist 能看案件但不能接手/处置（权限矩阵 docs/PRD.md §4.2）。"""
    result = make_case(case_db, event_id="EVT-1")
    headers = auth_headers(strategist)
    assert client.get("/api/v1/cases", headers=headers).status_code == 200
    assert client.get(f"/api/v1/cases/{result.case_no}", headers=headers).status_code == 200

    denied = client.post(f"/api/v1/cases/{result.case_no}/claim", headers=headers)
    assert denied.status_code == 403
    assert denied.json()["code"] == ErrorCode.PERMISSION_DENIED


def test_api_archive_requires_admin(
    client: TestClient, case_db, auditor: SysUser, admin: SysUser
) -> None:
    """归档是 admin 专属；单个归档与批量归档都要 403/200 分明。"""
    result = make_case(case_db, event_id="EVT-1")
    client.post(f"/api/v1/cases/{result.case_no}/claim", headers=auth_headers(auditor))
    client.post(
        f"/api/v1/cases/{result.case_no}/dispose",
        headers=auth_headers(auditor),
        json={"business_result": "reject", "risk_actions": ["pass"], "remark": _REMARK},
    )

    denied = client.post(f"/api/v1/cases/{result.case_no}/archive", headers=auth_headers(auditor))
    assert denied.status_code == 403

    ok = client.post(f"/api/v1/cases/{result.case_no}/archive", headers=auth_headers(admin))
    assert ok.status_code == 200
    assert ok.json()["data"]["case"]["status"] == CASE_ARCHIVED


def test_api_close_requires_reason(client: TestClient, case_db, admin: SysUser) -> None:
    """强制关闭必须填原因；关闭后状态为 closed。"""
    result = make_case(case_db, event_id="EVT-1")
    headers = auth_headers(admin)

    invalid = client.post(
        f"/api/v1/cases/{result.case_no}/close", headers=headers, json={"reason": "x"}
    )
    assert invalid.status_code == 422

    ok = client.post(
        f"/api/v1/cases/{result.case_no}/close",
        headers=headers,
        json={"reason": "重复建案，人工关闭"},
    )
    assert ok.status_code == 200
    assert ok.json()["data"]["status"] == "closed"


def test_api_dispose_records_items_for_ui(
    client: TestClient, case_db, auditor: SysUser
) -> None:
    """处置响应逐项返回联动结果，前端据此渲染"动作执行结果"列表。"""
    result = make_case(case_db, event_id="EVT-1")
    headers = auth_headers(auditor)
    client.post(f"/api/v1/cases/{result.case_no}/claim", headers=headers)

    body = client.post(
        f"/api/v1/cases/{result.case_no}/dispose",
        headers=headers,
        json={
            "business_result": "reject",
            "risk_actions": ["blacklist_user", "watchlist_add"],
            "remark": _REMARK,
        },
    ).json()["data"]

    actions = [item["risk_action"] for item in body["items"]]
    assert actions == ["blacklist_user", "watchlist_add"]
    assert body["items"][0]["target_id"] == "U1"
    assert body["items"][0]["exec_result"] == "success"
