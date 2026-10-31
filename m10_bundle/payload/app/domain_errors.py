"""M8 业务错误。M9 再统一所有接口的错误响应契约。"""


class BusinessError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


STATUS_BY_CODE = {
    "member_not_found": 404, "draft_not_found": 404,
    "report_not_found": 404, "trace_not_found": 404,
    "source_changed": 409, "report_conflict": 409,
    "already_confirmed_changed": 409, "empty_chunks": 422,
    "index_budget_exceeded": 504, "invalid_embedding": 502,
    "source_not_found": 404, "version_conflict": 409,
    "idempotency_conflict": 409, "upload_in_progress": 409,
    "duplicate_upload": 409, "quality_rule_violation": 422,
}


def error_status(error):
    return STATUS_BY_CODE.get(error.code, 500)
