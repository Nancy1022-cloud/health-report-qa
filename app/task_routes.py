"""M11 异步入口；M10 同步入口保留用于兼容既有调用方和回归。"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile

from app.dependencies import get_task_service
from app.report_extract import MAX_BYTES
from app.schemas.governance import RevisionRequest
from app.schemas.responses import ERROR_RESPONSES
from app.schemas.tasks import TaskResponse, WorkerResponse
from app.upload_models import Review
from app.upload_routes import Member, guarded

router = APIRouter(tags=['M11 持久化处理任务'], responses=ERROR_RESPONSES)
Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')]


@router.get('/api/tasks/worker', response_model=WorkerResponse)
@guarded
def worker(service=Depends(get_task_service)):
    return service.repository.worker_status()


@router.post('/api/tasks/upload', status_code=202, response_model=TaskResponse, response_model_exclude_unset=True)
@guarded
def upload(file: Annotated[UploadFile, File()], member_id: Annotated[str, Form(min_length=1, max_length=80)],
           idempotency_key: Key, service=Depends(get_task_service)):
    try:
        return service.upload(member_id, file.filename or '', file.file.read(MAX_BYTES + 1), idempotency_key)
    finally:
        file.file.close()


@router.post('/api/tasks/reports/{report_id}/confirm', status_code=202, response_model=TaskResponse, response_model_exclude_unset=True)
@guarded
def confirm(report_id: str, member_id: Member, body: Review, idempotency_key: Key, service=Depends(get_task_service)):
    return service.confirm(report_id, member_id, body, idempotency_key)


@router.post('/api/tasks/reports/{report_id}/revisions', status_code=202, response_model=TaskResponse, response_model_exclude_unset=True)
@guarded
def revise(report_id: str, member_id: Member, body: RevisionRequest, idempotency_key: Key, service=Depends(get_task_service)):
    return service.revise(report_id, member_id, body, idempotency_key)


@router.get('/api/tasks/{task_id}', response_model=TaskResponse)
@guarded
def task(task_id: str, member_id: Member, service=Depends(get_task_service)):
    return service.repository.get(task_id, member_id)
