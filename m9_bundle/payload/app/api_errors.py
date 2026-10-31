"""统一 HTTP 边界。请求校验与输出契约违规使用不同错误码。"""

import re
from uuid import uuid4

import psycopg
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
from starlette.exceptions import HTTPException

from app.domain_errors import BusinessError, error_status
from app.observability import RequestLogMiddleware, configure_logging, emit
from app.schemas.responses import ERROR_RESPONSES, ErrorResponse

DEFAULTS = {
    400: ("bad_request", "请求格式不正确。"),
    404: ("resource_not_found", "资源不存在，或不属于本次指定成员。"),
    405: ("method_not_allowed", "此接口不支持该请求方法。"),
    409: ("state_conflict", "当前状态不允许此操作，请重新读取状态。"),
    413: ("payload_too_large", "上传内容超过限制。"),
    415: ("unsupported_file", "文件格式不支持。"),
    422: ("validation_error", "请求参数不符合接口约定。"),
    500: ("internal_error", "服务内部错误，请使用 request_id 定位。"),
    502: ("upstream_error", "外部模型响应失败。"),
    503: ("dependency_unavailable", "数据库、存储或模型配置暂不可用。"),
    504: ("upstream_timeout", "外部请求或本次处理预算超时。"),
}
# 可向客户端保留的稳定旧业务码；不会透传供应商响应、任意 detail 或原始参数。
KNOWN_CODES = {
    "member_not_found", "draft_not_found", "report_not_found", "trace_not_found", "source_changed",
    "report_conflict", "already_confirmed_changed", "empty_chunks", "index_budget_exceeded",
    "invalid_embedding", "embedding_timeout", "embedding_http_error", "embedding_connection_error",
    "database_error", "storage_error", "invalid_model_or_config", "model_timeout", "model_http_error",
    "model_connection_error", "total_timeout", "tool_execution_failed", "invalid_model_output_or_config",
    "log_write_failed", "empty_file", "file_too_large", "unsupported_file", "invalid_image",
    "image_too_large", "encrypted_pdf", "page_limit", "pdf_stream_too_large", "pdf_needs_ocr",
    "text_limit", "invalid_pdf", "extraction_timeout", "extraction_http_error",
    "extraction_connection_error", "invalid_extraction", "invalid_image_text",
}


def error_response(request, status, code, message, details=None, exc=None, headers=None):
    state = request.scope.setdefault("state", {}).setdefault("m9", {"request_id": uuid4().hex})
    state.setdefault("request_id", uuid4().hex)
    state["error_code"], state["error_type"] = code, type(exc).__name__ if exc else None
    result = ErrorResponse(code=code, message=message, request_id=state["request_id"], details=details or [])
    return JSONResponse(result.model_dump(), status_code=status,
                        headers={**(headers or {}), "X-Request-ID": state["request_id"]},
                        media_type="application/json; charset=utf-8")


async def validation_handler(request, exc):
    # 不记录 input、ctx、body 或验证器异常文本，防止报告正文随失败响应/日志输出。
    def field_name(location):
        return ".".join(str(part) if isinstance(part, int) else
                        re.sub(r"[^a-zA-Z0-9_.-]", "?", str(part))[:80] for part in location)
    issues = [{"field": field_name(item["loc"]), "type": item["type"]} for item in exc.errors()[:30]]
    return error_response(request, 422, "validation_error", DEFAULTS[422][1], issues, exc)


async def response_handler(request, exc):
    fields = [{"field": ".".join(str(p) for p in issue["loc"]), "type": issue["type"]}
              for issue in exc.errors()[:30]]
    emit("contract.violation", fields=fields)
    return error_response(request, 500, "response_contract_violation", "服务返回值不符合输出契约，请使用 request_id 定位。", exc=exc)


async def business_handler(request, exc):
    return error_response(request, error_status(exc), exc.code, exc.message, exc=exc)


async def http_handler(request, exc):
    code, message = DEFAULTS.get(exc.status_code, ("http_error", "请求未完成。"))
    if isinstance(exc.detail, dict) and exc.detail.get("code") in KNOWN_CODES:
        code = exc.detail["code"]
        message = str(exc.detail.get("message") or message)
    if isinstance(exc.detail, dict):
        query_id = str(exc.detail.get("query_id", ""))
        if re.fullmatch(r"[a-f0-9-]{36}", query_id):
            request.scope.setdefault("state", {}).setdefault("m9", {})["query_id"] = query_id
    # 旧 RAG request_404/request_409 按 HTTP 类别稳定归一，不把旧消息当机器码。
    return error_response(request, exc.status_code, code, message, exc=exc, headers=exc.headers)


async def dependency_handler(request, exc):
    if isinstance(exc, APITimeoutError):
        status, code = 504, "upstream_timeout"
    elif isinstance(exc, (APIStatusError, APIConnectionError, APIError)):
        status, code = 502, "upstream_error"
    elif isinstance(exc, psycopg.Error):
        status, code = 503, "database_error"
    else:
        status, code = 503, "storage_error"
    return error_response(request, status, code, DEFAULTS[status][1], exc=exc)


async def internal_handler(request, exc):
    return error_response(request, 500, "internal_error", DEFAULTS[500][1], exc=exc)


def install_api_contract(app, *, configure=True):
    if getattr(app.state, "m9_installed", False):
        return
    if configure:
        configure_logging()
    app.add_middleware(RequestLogMiddleware)
    for exception, handler in [(RequestValidationError, validation_handler),
            (ResponseValidationError, response_handler), (BusinessError, business_handler),
            (HTTPException, http_handler), (psycopg.Error, dependency_handler),
            (APIError, dependency_handler), (OSError, dependency_handler), (Exception, internal_handler)]:
        app.add_exception_handler(exception, handler)
    app.state.m9_installed = True
