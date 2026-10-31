"""M10 修订和来源接口的显式契约。actor 是本地操作标记，不是认证身份。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator

from app.schemas.responses import Contract
from app.upload_models import Review


class RevisionRequest(Contract):
    expected_version: StrictInt = Field(ge=1)
    rebuild_index: StrictBool = False
    reason: str = Field(min_length=1, max_length=500)
    actor: str = Field(default="local_user", min_length=1, max_length=80)
    review: Review

    @field_validator("reason", "actor")
    @classmethod
    def meaningful(cls, value):
        if not value.strip():
            raise ValueError("不可为空白")
        return value.strip()


class RevisionReceipt(Contract):
    status: Literal["confirmed"]
    report_id: str
    member_id: str
    report_version: int = Field(ge=1)
    index_version: int = Field(ge=1)
    index_state: Literal["ready"]
    replayed: bool
    changed: bool
    embedding_calls: int | None = Field(default=None, ge=0)
    token_usage: dict[str, Any] | None = None


class RevisionRecord(Contract):
    report_version: int
    origin: str
    actor: str
    reason: str
    created_at: datetime
    snapshot_sha256: str
    previous_sha256: str | None
    snapshot: dict[str, Any]
    metric_sources: list[str]


class ProvenanceResponse(Contract):
    report_id: str
    member_id: str
    current_version: int
    index_version: int
    index_state: str
    original_source: dict[str, Any] | None
    original_extraction_available: bool
    revisions: list[RevisionRecord]


class SourceResponse(Contract):
    source_id: str
    report_id: str
    member_id: str
    report_version: int
    current_version: int
    is_current: bool
    source_kind: str
    payload: dict[str, Any]
    provenance_url: str
