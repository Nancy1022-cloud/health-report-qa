"""仅为已迁移的业务错误保留 M7 HTTP 形状，不是 M9 的统一异常系统。"""

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
            detail = exc.message if scalar else {"code": exc.code, "message": exc.message}
            raise HTTPException(error_status(exc), detail) from None
    return wrapped
