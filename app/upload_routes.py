"""M6 独立上传入口；继续使用 M5 的 /api/agent/query 问答。"""

import logging
from functools import wraps
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
from psycopg.rows import dict_row

from app.db import connect_db
from app.report_extract import MAX_BYTES, fail
from app.report_ingest import confirm_report, create_draft, get_draft, load_draft
from app.upload_models import Review

router = APIRouter(tags=["M6 上传与核对"])
logger = logging.getLogger(__name__)
Member = Annotated[str, Query(min_length=1, max_length=80)]


def guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
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


@router.get("/api/ingest/members")
@guarded
def members():
    with connect_db() as conn:
        conn.row_factory = dict_row
        return conn.execute("SELECT id, name FROM family_members ORDER BY id").fetchall()


@router.post("/api/ingest/upload")
@guarded
def upload(file: Annotated[UploadFile, File()], member_id: Annotated[str, Form(min_length=1, max_length=80)]):
    try:
        data = file.file.read(MAX_BYTES + 1)
        return create_draft(member_id, file.filename or "", data)
    finally:
        file.file.close()


@router.get("/api/ingest/{report_id}")
@guarded
def detail(report_id: str, member_id: Member):
    return get_draft(report_id, member_id)


@router.get("/api/ingest/{report_id}/file")
@guarded
def original(report_id: str, member_id: Member):
    draft, path = load_draft(report_id, member_id)
    return FileResponse(path, media_type="application/pdf" if draft["extension"] == ".pdf" else
                        "image/png" if draft["extension"] == ".png" else "image/jpeg",
                        filename="report" + draft["extension"], content_disposition_type="inline",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/api/ingest/{report_id}/confirm")
@guarded
def confirm(report_id: str, member_id: Member, body: Review):
    return confirm_report(report_id, member_id, body)