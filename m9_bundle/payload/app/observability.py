"""M9：请求关联、阶段耗时、JSON 日志。业务字段不进入请求日志。"""

import inspect
import json
import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

context = ContextVar("health_request", default=None)
logger = logging.getLogger("health.requests")


def configure_logging():
    if getattr(logger, "m9_configured", False):
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    destination = os.getenv("M9_LOG_PATH", "app/data/m9/runtime/requests.jsonl")
    if destination:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.m9_configured = True


def emit(event, **fields):
    state = context.get()
    if state is None:
        return
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
              "request_id": state["request_id"], **fields}
    logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


@contextmanager
def stage(name):
    state = context.get()
    if state is None:
        yield
        return
    begin, outcome = time.perf_counter(), "ok"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        duration = round((time.perf_counter() - begin) * 1000, 3)
        state["stages"].append({"stage": name, "duration_ms": duration, "outcome": outcome})
        emit("stage.completed", stage=name, duration_ms=duration, outcome=outcome)


def observed(name):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def wrapped(*args, **kwargs):
                with stage(name):
                    return await function(*args, **kwargs)
        else:
            @wraps(function)
            def wrapped(*args, **kwargs):
                with stage(name):
                    return function(*args, **kwargs)
        return wrapped
    return decorate


def business_result(result, *, no_records=False):
    status = result.get("status")
    code = {"answered": "ok", "retrieved": "retrieval_only", "partial": "partial_answer",
            "clarify": "clarification_required", "insufficient":
            "no_records" if no_records else "insufficient_evidence"}.get(status)
    result = {**result, "result_code": code}
    state = context.get()
    if state is not None:
        state["business_status"] = status
        state["result_code"] = code
        state["query_id"] = result.get("query_id")
    return result


class RequestLogMiddleware:
    """纯 ASGI：不读取 body，不接受客户端伪造的关联 ID。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = {"request_id": uuid4().hex, "stages": [], "error_code": None, "error_type": None}
        scope.setdefault("state", {})["m9"] = state
        token = context.set(state)
        begin, status, complete = time.perf_counter(), 500, False

        async def logged_send(message):
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-request-id"]
                headers.append((b"x-request-id", state["request_id"].encode("ascii")))
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True
            await send(message)
        try:
            await self.app(scope, receive, logged_send)
        except Exception as exc:
            # 未处理错误由外层 ServerErrorMiddleware + 全局处理器发送 500。
            state["error_code"], state["error_type"] = "internal_error", type(exc).__name__
            raise
        finally:
            route = getattr(scope.get("route"), "path", "<unmatched>")
            emit("request.completed", method=scope["method"], route=route, http_status=status,
                 duration_ms=round((time.perf_counter() - begin) * 1000, 3),
                 error_code=state["error_code"], error_type=state["error_type"],
                 business_status=state.get("business_status"), result_code=state.get("result_code"),
                 query_id=state.get("query_id"), stages=state["stages"], response_body_complete=complete)
            context.reset(token)
