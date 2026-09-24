"""统一错误码与领域异常。

契约（docs/ARCHITECTURE.md §9）：所有接口的错误响应结构一致 ——
``{code, message, field, detail}``，其中 ``code`` 是**稳定的机器码**，
``message`` 是面向人的中文说明。

错误码分段：

===== ========== ==========================================
分段   含义       例子
===== ========== ==========================================
0     成功       ok
400xx 参数/校验  PARAM_INVALID / EVENT_TIME_INVALID
401xx 鉴权失败   AUTH_REQUIRED / API_KEY_INVALID
403xx 权限不足   PERMISSION_DENIED
404xx 资源不存在 EVENT_ORDER_NOT_FOUND
409xx 冲突       EVENT_DUPLICATED
500xx 服务端异常 SYSTEM_ERROR / DEPENDENCY_UNAVAILABLE
===== ========== ==========================================

**服务层只抛 ``BusinessError``，不导入 FastAPI 类型**（AGENTS.md §6.3、
docs/ARCHITECTURE.md §9）：异常到 HTTP 状态的映射集中在 ``app/api/errors.py`` 一处，
服务层因此可以被脚本与测试直接调用而不需要拉起 Web 框架。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """错误码常量。前缀即 HTTP 分段，见模块文档。"""

    OK = "0"

    # ---- 400xx 参数与校验 ----
    PARAM_INVALID = "40001"
    EVENT_TYPE_UNSUPPORTED = "40002"
    EVENT_TIME_INVALID = "40003"
    EVENT_PAYLOAD_INVALID = "40004"
    BATCH_TOO_LARGE = "40005"
    EXPRESSION_INVALID = "40006"

    # ---- 401xx 鉴权 ----
    AUTH_REQUIRED = "40101"
    TOKEN_INVALID = "40102"
    TOKEN_EXPIRED = "40103"
    API_KEY_MISSING = "40104"
    API_KEY_INVALID = "40105"
    ACCOUNT_DISABLED = "40106"
    CREDENTIAL_INVALID = "40107"

    # ---- 403xx 权限 ----
    PERMISSION_DENIED = "40301"

    # ---- 404xx 资源不存在 ----
    RESOURCE_NOT_FOUND = "40401"
    EVENT_ORDER_NOT_FOUND = "40402"
    EVENT_REFUND_NOT_FOUND = "40403"

    # ---- 409xx 冲突 ----
    EVENT_DUPLICATED = "40901"
    STATE_CONFLICT = "40902"

    # ---- 500xx 服务端 ----
    SYSTEM_ERROR = "50001"
    DEPENDENCY_UNAVAILABLE = "50002"


# 错误码 -> 建议的 HTTP 状态码。集中在此，接口层不必逐个 if。
HTTP_STATUS_BY_CODE: dict[str, int] = {
    ErrorCode.OK: 200,
    ErrorCode.PARAM_INVALID: 422,
    ErrorCode.EVENT_TYPE_UNSUPPORTED: 422,
    ErrorCode.EVENT_TIME_INVALID: 422,
    ErrorCode.EVENT_PAYLOAD_INVALID: 422,
    ErrorCode.BATCH_TOO_LARGE: 413,
    ErrorCode.EXPRESSION_INVALID: 422,
    ErrorCode.AUTH_REQUIRED: 401,
    ErrorCode.TOKEN_INVALID: 401,
    ErrorCode.TOKEN_EXPIRED: 401,
    ErrorCode.API_KEY_MISSING: 401,
    ErrorCode.API_KEY_INVALID: 401,
    ErrorCode.ACCOUNT_DISABLED: 403,
    ErrorCode.CREDENTIAL_INVALID: 401,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.EVENT_ORDER_NOT_FOUND: 404,
    ErrorCode.EVENT_REFUND_NOT_FOUND: 404,
    ErrorCode.EVENT_DUPLICATED: 409,
    ErrorCode.STATE_CONFLICT: 409,
    ErrorCode.SYSTEM_ERROR: 500,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
}


class BusinessError(Exception):
    """业务异常基类。

    ``field`` 用点号路径表达字段位置（如 ``payload.order_no``），
    前端可以直接把错误挂到对应表单项上，而不是只弹一个笼统的 toast。
    """

    code: str = ErrorCode.SYSTEM_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        field: str | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.field = field
        self.detail = detail

    @property
    def http_status(self) -> int:
        return HTTP_STATUS_BY_CODE.get(self.code, 500)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "detail": self.detail,
        }


class ValidationError(BusinessError):
    """入参/业务校验失败（400xx）。"""

    code = ErrorCode.PARAM_INVALID


class AuthError(BusinessError):
    """鉴权失败（401xx）。"""

    code = ErrorCode.AUTH_REQUIRED


class PermissionError_(BusinessError):
    """权限不足（403xx）。

    名字带下划线是为了不与内置的 ``PermissionError`` 撞名 ——
    撞名会让 ``except PermissionError`` 意外捕获本类，
    混进"文件权限错误"与"角色权限不足"两种完全不同的语义。
    """

    code = ErrorCode.PERMISSION_DENIED


class NotFoundError(BusinessError):
    """资源不存在（404xx）。"""

    code = ErrorCode.RESOURCE_NOT_FOUND


class ConflictError(BusinessError):
    """状态/幂等冲突（409xx）。"""

    code = ErrorCode.STATE_CONFLICT
