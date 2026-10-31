"""M8：HTTP 参数与业务服务的薄路由。"""

from fastapi import APIRouter, Depends, Query

from app.dependencies import database_connection, get_record_service
from app.http_errors import business_errors
from app.repositories.records import RecordRepository
from app.services.records import RecordService

router = APIRouter(prefix="/api", tags=["M1 数据"])


@business_errors(scalar=True)
def require_member(conn, member_id):
    """保留旧的显式调用入口；新路由使用注入的 Service。"""
    return RecordService(RecordRepository(conn)).require_member(member_id)


@router.get("/members")
@business_errors(scalar=True)
def list_members(service=Depends(get_record_service)):
    return service.members()


@router.get("/reports")
@business_errors(scalar=True)
def list_reports(member_id: str = Query(min_length=1, max_length=80), service=Depends(get_record_service)):
    return service.reports(member_id)


@router.get("/reports/{report_id}")
@business_errors(scalar=True)
def get_report(report_id: str, member_id: str = Query(min_length=1, max_length=80),
               service=Depends(get_record_service)):
    return service.report(report_id, member_id)


@router.get("/metrics")
@business_errors(scalar=True)
def list_metrics(member_id: str = Query(min_length=1, max_length=80),
                 metric_code: str | None = Query(default=None, min_length=1, max_length=80),
                 service=Depends(get_record_service)):
    return service.metrics(member_id, metric_code)
