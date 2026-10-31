"""从原路由原样迁移的请求模型；响应契约在 M9 补齐。"""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_TOP_K = 2
MemberID = Annotated[str, Field(min_length=1, max_length=80)]
ReportID = Annotated[str, Field(min_length=1, max_length=160)]

class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=1000)
    member_id: str = Field(min_length=1, max_length=80)
    report_ids: list[str] | None = Field(default=None, min_length=1, max_length=20)
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=8)
    chunk_version: Literal["fixed200o40", "section200o40v1"] = "fixed200o40"
    retrieval_mode: Literal["vector", "keyword", "hybrid"] = "vector"
    with_answer: bool = True

    @field_validator("question", "member_id")
    @classmethod
    def trim_required(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("不能只填写空格。")
        return value

    @field_validator("report_ids")
    @classmethod
    def check_report_ids(cls, value):
        if value is not None and any(not item.strip() or len(item) > 160 for item in value):
            raise ValueError("报告 ID 不能为空或超过 160 字符。")
        return list(dict.fromkeys(item.strip() for item in value)) if value is not None else None


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=1000)
    member_ids: list[MemberID] = Field(default_factory=list, max_length=5)
    report_ids: list[ReportID] | None = Field(default=None, min_length=1, max_length=40)
    chunk_version: Literal["fixed200o40", "section200o40v1"] = "section200o40v1"
    retrieval_mode: Literal["vector", "keyword", "hybrid"] = "hybrid"

    @field_validator("member_ids", "report_ids")
    @classmethod
    def unique_ids(cls, value):
        return list(dict.fromkeys(item.strip() for item in value)) if value is not None else None

    @model_validator(mode="after")
    def reports_need_members(self):
        if self.report_ids is not None and not self.member_ids:
            raise ValueError("指定报告时必须同时指定 member_ids。")
        return self
