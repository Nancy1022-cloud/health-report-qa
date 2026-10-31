"""M11 有限任务策略；不把未知调用用量算成零。"""

import os
from dataclasses import dataclass

from fastapi import HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.domain_errors import BusinessError


class LostExecution(Exception):
    """租约、执行标识或处理版本已失效；本执行禁止再提交。"""


class TaskFailure(Exception):
    def __init__(self, code, *, retryable=False, usage=None):
        self.code, self.retryable, self.usage = code, retryable, usage
        super().__init__(code)


@dataclass(frozen=True)
class TaskPolicy:
    max_attempts: int = 3
    max_calls: int = 128
    timeout_seconds: int = 600
    lease_seconds: int = 15
    heartbeat_seconds: float = 3
    call_timeout_seconds: int = 30
    min_call_interval: float = 0.25

    def __post_init__(self):
        if not (1 <= self.max_attempts <= 5 and 1 <= self.max_calls <= 256 and
                5 <= self.timeout_seconds <= 1800 and 3 <= self.lease_seconds <= 60 and
                0 < self.heartbeat_seconds < self.lease_seconds / 2 and
                1 <= self.call_timeout_seconds <= 60 and 0 <= self.min_call_interval <= 10):
            raise ValueError("M11 任务策略超出允许范围")

    @classmethod
    def from_env(cls):
        return cls(max_attempts=int(os.getenv('M11_MAX_ATTEMPTS', '3')),
                   max_calls=int(os.getenv('M11_MAX_CALLS', '128')),
                   timeout_seconds=int(os.getenv('M11_TASK_TIMEOUT', '600')),
                   min_call_interval=float(os.getenv('M11_CALL_INTERVAL', '0.25')))


def backoff(attempt):
    return min(2 ** max(0, attempt - 1), 8)


def classify(error):
    if isinstance(error, TaskFailure):
        return error
    if isinstance(error, APITimeoutError):
        return TaskFailure('upstream_timeout', retryable=True)
    if isinstance(error, APIConnectionError):
        return TaskFailure('upstream_connection', retryable=True)
    if isinstance(error, APIStatusError):
        code = str(getattr(error, 'code', '') or '').lower()
        quota = any(word in code for word in ('quota', 'arrearage', 'balance', 'permission'))
        return TaskFailure('upstream_http_' + str(error.status_code),
                           retryable=not quota and error.status_code in {408, 429, 500, 502, 503, 504})
    if isinstance(error, BusinessError):
        return TaskFailure(error.code)
    if isinstance(error, HTTPException):
        detail = error.detail
        return TaskFailure(detail.get('code', 'invalid_input') if isinstance(detail, dict) else 'configuration_or_input')
    # 文件、数据库和未知编程错误不盲目重放；进程异常退出由租约恢复处理。
    return TaskFailure('storage_error' if isinstance(error, OSError) else 'internal_error')


def processing_config():
    return {name: os.getenv(name, '') for name in (
        'LLM_MODEL', 'LLM_BASE_URL', 'VISION_MODEL', 'VISION_BASE_URL',
        'EMBEDDING_MODEL', 'EMBEDDING_BASE_URL', 'EMBEDDING_DIMENSIONS')}
