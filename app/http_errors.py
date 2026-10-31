"""业务异常适配：兼容旧调用入口，完整保留业务码供统一处理器读取。"""

from functools import wraps

from fastapi import HTTPException

from app.domain_errors import BusinessError, error_status


def business_errors(function=None, *, scalar=False):
    if function is None:
        return lambda target: business_errors(target, scalar=scalar)

    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BusinessError as exc:
            detail = {"code": exc.code, "message": exc.message}
            raise HTTPException(error_status(exc), detail) from None
    return wrapped
