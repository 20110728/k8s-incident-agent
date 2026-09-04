from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import (
    HTTPException as StarletteHTTPException,
)

from backend.app.api.schemas import (
    ErrorDetail,
    ErrorResponse,
)


class ApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(
            code=code,
            message=message,
            details=details,
        )
    )

    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(
            body.model_dump(mode="json")
        ),
    )


def register_exception_handlers(
    app: FastAPI,
) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(
        request: Request,
        error: ApiError,
    ) -> JSONResponse:
        del request
        return _error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            details=error.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del request
        return _error_response(
            status_code=422,
            code="REQUEST_VALIDATION_ERROR",
            message="Request validation failed.",
            details=error.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        del request
        code = (
            "RESOURCE_NOT_FOUND"
            if error.status_code == 404
            else "HTTP_ERROR"
        )
        message = (
            str(error.detail)
            if error.detail
            else "HTTP request failed."
        )
        return _error_response(
            status_code=error.status_code,
            code=code,
            message=message,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        del request, error
        return _error_response(
            status_code=500,
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected server error occurred.",
        )