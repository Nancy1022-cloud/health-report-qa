"""M4 新入口；M2/M3 的 /api/query 保持原样。"""

from typing import Annotated, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent import run_agent

router = APIRouter(prefix="/api/agent", tags=["M4 工具调用"])
MemberID = Annotated[str, Field(min_length=1, max_length=80)]
ReportID = Annotated[str, Field(min_length=1, max_length=160)]


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
        return list(dict.fromkeys(value)) if value is not None else None

    @model_validator(mode="after")
    def reports_need_members(self):
        if self.report_ids is not None and not self.member_ids:
            raise ValueError("指定报告时必须同时指定 member_ids。")
        return self


@router.post("/query")
async def ask_agent(body: AgentRequest):
    return await run_agent(body.question, body.member_ids, body.report_ids,
                           chunk_version=body.chunk_version, retrieval_mode=body.retrieval_mode)