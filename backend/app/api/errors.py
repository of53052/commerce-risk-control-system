"""全局异常处理器：把各类异常统一映射成 §9 约定的错误响应。

为什么必须集中处理（而不是每个接口自己 try/except）：

1. **一致性**：前端只需要写一套错误处理逻辑；分散处理必然出现某个接口
   返回 ``{"detail": ...}``、另一个返回 ``{"error": ...}``，前端只能靠猜。
2. **不漏网**：未捕获异常会变成 FastAPI 默认的 500 HTML/纯文本，
   连 JSON 都不是。集中兜底能保证"任何情况下响应都是 JSON 且带 trace_id"，
   这条对排障极其重要 —— 500 时 trace_id 是唯一的线索。
3. **不泄露内部细节**：``RequestValidationError`` 的原始结构包含 Python 类型对象，
   直接返回给前端既不友好也不安全，必须转成「字段路径 + 原因」。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from app.api.response import failure
from app.core.logging import log_kv, trace_id_ctx
from app.services.errors import (
    ErrorCode,
    BusinessError,
    ConflictError,
    ValidationError,
)

logger = logging.getLogger("app.api.errors")


def register_exception_handlers(app: FastAPI) -> None:
    """把所有处理器挂到应用上（在 main.py 里调用一次）。"""

    @app.exception_handler(BusinessError)
    async def _business_error(_: Request, exc: BusinessError) -> JSONResponse:
        # 业务异常是「预期内的失败」，用 WARNING 而不是 ERROR：
        # ERROR 级别应该留给"我们没预料到的问题"，否则告警会被噪声淹没。
        log_kv(
            logger,
            logging.WARNING,
            "业务异常",
            code=exc.code,
            field=exc.field,
            detail=exc.detail,
            err=exc.message,
        )
        return JSONResponse(status_code=exc.http_status, content=failure(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic 结构校验失败 -> 42201，并给出第一个出错字段的路径。

        只返回**第一个**错误：批量场景下一次列出几十条错误对使用者没有价值，
        修完第一个再报下一个是更自然的交互；完整列表放在 detail 里供调用方按需查看。
        """
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = [str(part) for part in first.get("loc", []) if part not in {"body", "query", "path"}]
        message = str(first.get("msg") or "请求参数不合法")
        err = ValidationError(message, field=".".join(location) or None, detail=_jsonable(errors))
        return JSONResponse(status_code=err.http_status, content=failure(err))

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        """数据库唯一约束/外键冲突 -> 409。

        到这一层说明服务层没有把可预期的冲突转成业务异常，属于兜底；
        记 ERROR 日志并在 message 里明确"未预期的冲突"，不要假装是普通的业务校验失败。
        """
        log_kv(logger, logging.ERROR, "数据库完整性约束冲突（兜底路径）", err=str(exc.orig)[:300])
        err = ConflictError("数据冲突，请检查唯一键后重试", code=ErrorCode.STATE_CONFLICT)
        return JSONResponse(status_code=err.http_status, content=failure(err))

    @app.exception_handler(OperationalError)
    async def _operational_error(_: Request, exc: OperationalError) -> JSONResponse:
        """数据库不可用 -> 503。

        与 500 区分开：503 是"临时不可用，稍后重试可能成功"（MySQL 没启动、
        连接数打满），客户端与网关的重试策略应当不同。
        """
        log_kv(logger, logging.ERROR, "数据库连接不可用", err=str(exc.orig)[:300])
        err = BusinessError(
            "数据库暂时不可用，请稍后重试",
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
        )
        return JSONResponse(status_code=err.http_status, content=failure(err))

    @app.exception_handler(SQLAlchemyError)
    async def _sqlalchemy_error(_: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception("数据库操作异常")
        err = BusinessError("数据访问异常", code=ErrorCode.SYSTEM_ERROR, detail=str(exc)[:300])
        return JSONResponse(status_code=err.http_status, content=failure(err))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # 未预期异常：日志必须带完整堆栈，响应里**不带**（避免泄露内部结构）。
        logger.exception("未处理异常：%s", exc)
        err = BusinessError(
            f"服务内部错误，请联系管理员并提供 trace_id {trace_id_ctx.get()}",
            code=ErrorCode.SYSTEM_ERROR,
        )
        return JSONResponse(status_code=err.http_status, content=failure(err))


def _jsonable(errors: list[dict]) -> list[dict]:
    """把 Pydantic 错误里的非 JSON 对象（如 ValueError 实例）转成字符串。"""
    cleaned: list[dict] = []
    for item in errors:
        row = {key: value for key, value in item.items() if key != "ctx"}
        row["loc"] = [str(part) for part in item.get("loc", [])]
        cleaned.append(row)
    return cleaned
