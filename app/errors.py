"""统一错误模型与 FastAPI 异常处理器。

错误体格式（所有业务错误一致）::

    {"code": "QUOTE_AMOUNT_READONLY", "message": "...", "field": "final_monthly_cents"}
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

# --- 错误码常量 ---------------------------------------------------------------
QUOTE_AMOUNT_READONLY = "QUOTE_AMOUNT_READONLY"
QUOTE_NOT_FOUND = "QUOTE_NOT_FOUND"
QUOTE_NOT_EDITABLE = "QUOTE_NOT_EDITABLE"
QUOTE_NOT_REVISABLE = "QUOTE_NOT_REVISABLE"
QUOTE_NOT_SENDABLE = "QUOTE_NOT_SENDABLE"
QUOTE_VOID_REASON_REQUIRED = "QUOTE_VOID_REASON_REQUIRED"
QUOTE_STATUS_INVALID = "QUOTE_STATUS_INVALID"
LEAD_NOT_FOUND = "LEAD_NOT_FOUND"
RULE_SET_NOT_FOUND = "RULE_SET_NOT_FOUND"
NO_ACTIVE_RULE_SET = "NO_ACTIVE_RULE_SET"
INVALID_INPUT_SNAPSHOT = "INVALID_INPUT_SNAPSHOT"
VALIDATION_ERROR = "VALIDATION_ERROR"


class ApiError(Exception):
    """业务异常，直接映射为 HTTP 响应。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        field: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.field = field
        self.extra = extra or {}

    def body(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field:
            payload["field"] = self.field
        if self.extra:
            payload["extra"] = self.extra
        return payload


def amount_readonly(field: str, path: str = "") -> ApiError:
    return ApiError(
        status_code=422,
        code=QUOTE_AMOUNT_READONLY,
        message=(
            "金额与比例结果字段只允许由计算引擎产生，请求体中不得出现；"
            "如需调整请修改输入快照（座位数 / 合约月数 / 方案）由引擎重算。"
        ),
        field=field,
        extra={"path": path or field},
    )


def install_handlers(app) -> None:
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(ApiError)
    async def _api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": VALIDATION_ERROR,
                "message": "请求参数校验失败",
                "errors": [
                    {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
                    for e in exc.errors()
                ],
            },
        )
