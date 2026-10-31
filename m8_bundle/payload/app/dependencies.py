"""HTTP 组合根：在这里组装连接、Repository、Client 和 Service。"""

import logging

import psycopg
from fastapi import Depends, HTTPException
from psycopg.rows import dict_row

from app import agent, rag
from app.db import connect_db
from app.repositories.records import RecordRepository
from app.services.records import RecordService
from app.services.queries import QueryService

logger = logging.getLogger(__name__)


def database_connection():
    try:
        with connect_db() as conn:
            conn.row_factory = dict_row
            yield conn
    except psycopg.Error:
        logger.exception("M1 数据库读取失败")
        raise HTTPException(status_code=503, detail="数据库读取失败，请查看 app 日志。") from None


def get_record_service(conn=Depends(database_connection)):
    return RecordService(RecordRepository(conn))


def get_query_service():
    return QueryService(rag.run_query, agent.run_agent)


def get_report_service():
    # 旧模块只留下兼容入口及组装；业务逻辑只有 services/reports.py 一份。
    from app.report_ingest import make_report_service
    return make_report_service()
