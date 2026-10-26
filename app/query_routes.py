from typing import Literal

"""M2：问答、索引统计和单次问答日志读取接口。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rag import DEFAULT_TOP_K, run_query
from app.record_routes import database_connection

router = APIRouter(prefix="/api", tags=["M2 基础 RAG"])


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
        return list(dict.fromkeys(value)) if value is not None else None


@router.post("/query")
def ask(body: QueryRequest):
    return run_query(body.question, body.member_id, body.report_ids, body.top_k,
                     with_answer=body.with_answer, chunk_version=body.chunk_version,
                     retrieval_mode=body.retrieval_mode)


@router.get("/index-stats")
def index_stats(conn=Depends(database_connection)):
    rows = conn.execute(
        "SELECT r.member_id, r.id AS report_id, r.report_date, c.chunk_version, "
        "count(c.id) AS chunks, count(c.embedding) AS vectors, count(c.search_vector) AS keyword_vectors, "
        "min(c.embedding_model) AS embedding_model, min(c.embedding_dimensions) AS dimensions "
        "FROM reports r LEFT JOIN report_chunks c ON c.report_id=r.id "
        "WHERE r.status='confirmed' GROUP BY r.id, c.chunk_version ORDER BY r.member_id, r.report_date, r.id, c.chunk_version"
    ).fetchall()
    return {"items": rows, "report_count": len({r["report_id"] for r in rows}),
            "chunk_count": sum(r["chunks"] for r in rows)}


@router.get("/query/{query_id}/trace")
def get_trace(query_id: str, conn=Depends(database_connection)):
    row = conn.execute("SELECT * FROM query_logs WHERE id = %s", (query_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到这条问答日志。")
    return row

# HEALTH_QA_M3_PATCH_V1
