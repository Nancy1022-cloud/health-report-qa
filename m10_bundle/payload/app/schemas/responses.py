"""M9 HTTP 输出契约。动态模型 usage/业务 trace 保持 JSON，不伪造未知字段。"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.upload_models import Metric


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldIssue(Contract):
    field: str
    type: str


class ErrorResponse(Contract):
    code: str
    message: str
    request_id: str
    details: list[FieldIssue] = Field(default_factory=list)


class Member(Contract):
    id: str
    name: str
    aliases: list[str]


class IngestMember(Contract):
    id: str
    name: str


class MemberList(Contract):
    items: list[Member]
    count: int = Field(ge=0)

    @model_validator(mode="after")
    def count_matches(self):
        if self.count != len(self.items):
            raise ValueError("count 与 items 不一致")
        return self


class Report(Contract):
    id: str
    member_id: str
    title: str
    report_date: date
    report_type: str
    source_type: Literal["synthetic", "uploaded"]
    status: Literal["confirmed"]


class ReportDetail(Report):
    raw_text: str


class ReportList(Contract):
    member: Member
    items: list[Report]
    count: int = Field(ge=0)
    result_code: Literal["ok", "no_records"]

    @model_validator(mode="after")
    def consistency(self):
        if self.count != len(self.items) or self.result_code != ("ok" if self.items else "no_records"):
            raise ValueError("记录数量/业务状态不一致")
        if any(item.member_id != self.member.id for item in self.items):
            raise ValueError("成员范围不一致")
        return self


class MetricRecord(Contract):
    report_version: int | None = None
    id: int
    report_id: str
    member_id: str
    report_date: date
    metric_code: str
    metric_name: str
    value: str
    unit: str
    reference_range: str | None
    source_text: str
    source_id: str


class MetricList(Contract):
    member: Member
    items: list[MetricRecord]
    count: int = Field(ge=0)
    result_code: Literal["ok", "no_records"]

    @model_validator(mode="after")
    def consistency(self):
        if self.count != len(self.items) or self.result_code != ("ok" if self.items else "no_records"):
            raise ValueError("记录数量/业务状态不一致")
        if any(item.member_id != self.member.id or item.source_id != f"metric:{item.id}" for item in self.items):
            raise ValueError("来源或成员范围不一致")
        return self


class IndexRecord(Contract):
    member_id: str
    report_id: str
    report_date: date
    chunk_version: str | None
    chunks: int = Field(ge=0)
    vectors: int = Field(ge=0)
    keyword_vectors: int = Field(ge=0)
    embedding_model: str | None
    dimensions: int | None


class IndexStats(Contract):
    items: list[IndexRecord]
    report_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)


class TraceResponse(Contract):
    stale_report_ids: list[str] = Field(default_factory=list)
    id: str
    question: str
    member_ids: list[str]
    strategy: str | None
    trace: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    latency_ms: int | None
    token_usage: dict[str, Any]
    error_code: str | None
    created_at: datetime


class DraftResponse(Contract):
    upload_reused: bool | None = None
    report_version: int | None = None
    index_version: int | None = None
    index_state: str | None = None
    id: str
    member_id: str
    extension: Literal[".png", ".jpg", ".pdf"]
    filename: str
    file_sha256: str
    title: str
    report_date: date | None
    report_type: str
    raw_text: str
    metrics: list[Metric]
    warnings: list[str]
    model_info: dict[str, Any]
    status: Literal["draft", "confirmed"]
    file_url: str
    extracted_text: str | None = None


class ConfirmationResponse(Contract):
    report_version: int | None = None
    index_version: int | None = None
    index_state: str | None = None
    replayed: bool | None = None
    status: Literal["confirmed"]
    report_id: str
    member_id: str
    already_confirmed: bool
    chunk_versions: list[str]
    chunks_written: int | None = Field(default=None, ge=0)
    metrics_written: int | None = Field(default=None, ge=0)
    embedding_calls: int | None = Field(default=None, ge=0)
    token_usage: dict[str, Any] | None = None


class Evidence(BaseModel):
    # report / metric / chunk 的附加字段由现有工具分别定义。
    model_config = ConfigDict(extra="allow")
    source_id: str = Field(min_length=1)
    report_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)


class QueryResponse(Contract):
    query_id: str
    status: Literal["answered", "insufficient", "retrieved"]
    result_code: Literal["ok", "no_records", "insufficient_evidence", "retrieval_only"]
    answer: str | None
    citations: list[Evidence]
    evidence: list[Evidence]
    latency_ms: int = Field(ge=0)
    token_usage: dict[str, Any]
    model_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def consistency(self):
        expected = {"answered": {"ok"}, "retrieved": {"retrieval_only"},
                    "insufficient": {"no_records", "insufficient_evidence"}}
        if self.result_code not in expected[self.status]:
            raise ValueError("status 与 result_code 不一致")
        if self.status == "answered" and (not self.answer or not self.citations):
            raise ValueError("已回答必须包含正文与引用")
        if not {e.source_id for e in self.citations}.issubset({e.source_id for e in self.evidence}):
            raise ValueError("引用不属于本次证据")
        return self


class AgentResponse(Contract):
    query_id: str
    status: Literal["answered", "partial", "insufficient", "clarify"]
    result_code: Literal["ok", "partial_answer", "no_records", "insufficient_evidence", "clarification_required"]
    answer: str
    citations: list[Evidence]
    evidence: list[Evidence]
    tool_calls: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    embedding_calls: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    token_usage: dict[str, Any]
    evidence_check: dict[str, Any]
    supplemental_calls: int = Field(ge=0)
    revision_calls: int = Field(ge=0)
    trace_url: str

    @model_validator(mode="after")
    def consistency(self):
        expected = {"answered": {"ok"}, "partial": {"partial_answer"},
                    "clarify": {"clarification_required"},
                    "insufficient": {"no_records", "insufficient_evidence"}}
        if self.result_code not in expected[self.status]:
            raise ValueError("status 与 result_code 不一致")
        if self.status in {"answered", "partial"} and not self.citations:
            raise ValueError("事实回答必须包含引用")
        if self.status == "clarify" and self.citations:
            raise ValueError("澄清不能冒充事实回答")
        if not {e.source_id for e in self.citations}.issubset({e.source_id for e in self.evidence}):
            raise ValueError("引用不属于本次证据")
        if self.trace_url != f"/api/query/{self.query_id}/trace":
            raise ValueError("trace_url 与 query_id 不一致")
        return self


class HealthResponse(Contract):
    status: Literal["ok"]
    stage: str
    database: dict[str, Any]


class LLMTestResponse(Contract):
    status: Literal["ok"]
    model: str
    reply: str
    usage: dict[str, Any] | None


class EmbeddingTestResponse(Contract):
    status: Literal["ok"]
    model: str
    dimensions: int = Field(gt=0)
    preview: list[float]
    usage: dict[str, Any] | None


ERROR_RESPONSES = {status: {"model": ErrorResponse, "description": description}
    for status, description in {400: "请求格式错误", 404: "资源不存在或不在成员范围", 405: "方法不支持",
    409: "业务状态冲突", 413: "文件超出限制", 415: "文件格式不支持", 422: "请求校验失败",
    500: "响应契约违规或内部错误", 502: "外部模型失败", 503: "依赖或配置不可用",
    504: "外部请求或总预算超时"}.items()}
