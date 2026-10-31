"""M6 独立上传入口；继续使用 M5 的 /api/agent/query 问答。"""

import logging
from functools import wraps
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError

from app.report_extract import MAX_BYTES, fail
from app.dependencies import get_record_service, get_report_service
from app.domain_errors import BusinessError, error_status
from app.upload_models import Review
from app.schemas.responses import DraftResponse, ConfirmationResponse, IngestMember, ERROR_RESPONSES
from app.schemas.governance import RevisionRequest, RevisionReceipt, ProvenanceResponse, SourceResponse

router = APIRouter(tags=["M6 上传与核对"], responses=ERROR_RESPONSES)
logger = logging.getLogger(__name__)
Member = Annotated[str, Query(min_length=1, max_length=80)]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


def guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BusinessError as exc:
            fail(error_status(exc), exc.code, exc.message)
        except HTTPException:
            raise
        except APITimeoutError:
            fail(504, "embedding_timeout", "Embedding 超时；未完成确认，可保留核对内容后重试。")
        except APIStatusError as exc:
            fail(502, "embedding_http_error", f"Embedding 服务返回 HTTP {exc.status_code}；未完成确认。")
        except APIConnectionError:
            fail(502, "embedding_connection_error", "Embedding 服务连接失败；未完成确认。")
        except psycopg.Error:
            logger.exception("M6 数据库操作失败")
            fail(503, "database_error", "数据库操作失败，请查看 app 日志；重试同一上传 ID 可检查是否已经确认。")
        except OSError:
            logger.exception("M6 文件操作失败")
            fail(503, "storage_error", "无法读取或保存上传文件，请检查 uploads 挂载和目录权限。")
        except (APIError, ValueError, KeyError, TypeError):
            logger.exception("M6 输出或配置校验失败")
            fail(502, "invalid_model_or_config", "模型输出或配置校验失败，请查看 app 日志。")
    return wrapped


@router.get("/upload", include_in_schema=False)
def upload_page():
    return FileResponse(Path(__file__).parent / "static" / "upload.html")


@router.get("/api/ingest/members", response_model=list[IngestMember])
@guarded
def members(service=Depends(get_record_service)):
    return service.ingest_members()


@router.post("/api/ingest/upload", response_model=DraftResponse, response_model_exclude_unset=True)
@guarded
def upload(file: Annotated[UploadFile, File()], member_id: Annotated[str, Form(min_length=1, max_length=80)],
           idempotency_key: IdempotencyKey = None,
           service=Depends(get_report_service)):
    try:
        data = file.file.read(MAX_BYTES + 1)
        return service.create_draft(member_id, file.filename or "", data, idempotency_key)
    finally:
        file.file.close()


@router.get("/api/ingest/{report_id}", response_model=DraftResponse, response_model_exclude_unset=True)
@guarded
def detail(report_id: str, member_id: Member, service=Depends(get_report_service)):
    return service.get_draft(report_id, member_id)


@router.get("/api/ingest/{report_id}/file", response_class=FileResponse, responses={200: {"description": "报告原件（PDF / PNG / JPEG）", "content": {"application/pdf": {}, "image/png": {}, "image/jpeg": {}}}})
@guarded
def original(report_id: str, member_id: Member, service=Depends(get_report_service)):
    draft, path = service.original(report_id, member_id)
    return FileResponse(path, media_type="application/pdf" if draft["extension"] == ".pdf" else
                        "image/png" if draft["extension"] == ".png" else "image/jpeg",
                        filename="report" + draft["extension"], content_disposition_type="inline",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/api/ingest/{report_id}/confirm", response_model=ConfirmationResponse, response_model_exclude_unset=True)
@guarded
def confirm(report_id: str, member_id: Member, body: Review, idempotency_key: IdempotencyKey = None, service=Depends(get_report_service)):
    return service.confirm(report_id, member_id, body, idempotency_key)


@router.post("/api/reports/{report_id}/revisions", response_model=RevisionReceipt, response_model_exclude_unset=True)
@guarded
def revise(report_id: str, member_id: Member, body: RevisionRequest,
           idempotency_key: IdempotencyKey = None, service=Depends(get_report_service)):
    return service.revise(report_id, member_id, body, idempotency_key)


@router.get("/api/reports/{report_id}/provenance", response_model=ProvenanceResponse)
@guarded
def provenance(report_id: str, member_id: Member, service=Depends(get_report_service)):
    return service.repository.provenance(report_id, member_id)


@router.get("/api/sources/resolve", response_model=SourceResponse)
@guarded
def source(source_id: Annotated[str, Query(min_length=1, max_length=500)], member_id: Member, service=Depends(get_report_service)):
    return service.repository.resolve_source(source_id, member_id)
