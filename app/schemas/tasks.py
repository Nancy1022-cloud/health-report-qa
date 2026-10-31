"""任务与业务状态分开；202 仅表示任务已接受。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.responses import Contract


class TaskResponse(Contract):
    id: str
    member_id: str
    report_id: str
    kind: Literal['extract', 'confirm', 'revise']
    generation: int = Field(ge=1)
    status: Literal['queued', 'running', 'succeeded', 'failed', 'superseded']
    stage: str
    attempts: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    calls_reserved: int = Field(ge=0)
    max_calls: int = Field(ge=1)
    deadline: datetime
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None
    error_code: str | None
    result: dict[str, Any] | None
    status_url: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    call_states: dict[str, int] = Field(default_factory=dict)
    saved_stages: list[str] = Field(default_factory=list)


class WorkerResponse(Contract):
    online: bool
    worker_id: str | None
    heartbeat_at: datetime | None
