"""M8 基础问答路由：请求模型、服务依赖、HTTP 返回。"""

from fastapi import APIRouter, Depends

from app.dependencies import get_query_service, get_record_service
from app.http_errors import business_errors
from app.schemas.queries import QueryRequest

router = APIRouter(prefix="/api", tags=["M2 基础 RAG"])


@router.post("/query")
def ask(body: QueryRequest, service=Depends(get_query_service)):
    return service.query(body)


@router.get("/index-stats")
@business_errors(scalar=True)
def index_stats(service=Depends(get_record_service)):
    return service.index_stats()


@router.get("/query/{query_id}/trace")
@business_errors(scalar=True)
def get_trace(query_id: str, service=Depends(get_record_service)):
    return service.trace(query_id)
