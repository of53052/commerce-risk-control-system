"""统一响应封装。

契约（docs/ARCHITECTURE.md §9）::

    { "code": "0", "message": "ok", "data": {...}, "trace_id": "..." }

**为什么成功也带 trace_id**：排障的起点几乎总是"用户说这次操作不对"，
而 trace_id 是唯一能把响应、日志、审计串起来的线索。让前端在报错时能直接把
trace_id 贴给运维，比"请描述一下你做了什么"高效一个量级。

``code`` 用字符串而非整数：错误码里有 ``40001`` 这类前导分段语义，
字符串形态便于将来扩展成 ``"40001.3"``（细化到子原因）而不破坏前端的分支判断。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import trace_id_ctx
from app.services.errors import ErrorCode, BusinessError


def success(data: Any = None, *, message: str = "ok") -> dict[str, Any]:
    """成功响应体。"""
    return {
        "code": ErrorCode.OK,
        "message": message,
        "data": data,
        "trace_id": trace_id_ctx.get(),
    }


def failure(error: BusinessError) -> dict[str, Any]:
    """失败响应体（结构体中 ``data`` 恒为 None，避免前端误读半成品数据）。"""
    return {
        "code": error.code,
        "message": error.message,
        "data": None,
        "trace_id": trace_id_ctx.get(),
        "field": error.field,
        "detail": error.detail,
    }


def page(items: list[Any], *, total: int, page_no: int, size: int) -> dict[str, Any]:
    """分页响应体（docs/ARCHITECTURE.md §9 的分页约定）。"""
    return {"items": items, "total": total, "page": page_no, "size": size}
