import logging

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from psycopg.rows import dict_row

from app.db import connect_db

router = APIRouter(prefix="/api", tags=["M1 数据"])
logger = logging.getLogger(__name__)


def database_connection():
    try:
        with connect_db() as conn:
            conn.row_factory = dict_row
            yield conn
    except psycopg.Error:
        logger.exception("M1 数据库读取失败")
        raise HTTPException(status_code=503, detail="数据库读取失败，请查看 app 日志。") from None


def require_member(conn, member_id):
    member = conn.execute(
        "SELECT id, name, aliases FROM family_members WHERE id = %s", (member_id,),
    ).fetchone()
    if member is None:
        raise HTTPException(status_code=404, detail="成员不存在。")
    return member


@router.get("/members")
def list_members(conn=Depends(database_connection)):
    rows = conn.execute("SELECT id, name, aliases FROM family_members ORDER BY id").fetchall()
    return {"items": rows, "count": len(rows)}


@router.get("/reports")
def list_reports(
    member_id: str = Query(min_length=1, max_length=80),
    conn=Depends(database_connection),
):
    member = require_member(conn, member_id)
    rows = conn.execute(
        "SELECT id, member_id, title, report_date, report_type, source_type, status "
        "FROM reports WHERE member_id = %s AND status = 'confirmed' ORDER BY report_date, id",
        (member_id,),
    ).fetchall()
    return {"member": member, "items": rows, "count": len(rows)}


@router.get("/reports/{report_id}")
def get_report(
    report_id: str,
    member_id: str = Query(min_length=1, max_length=80),
    conn=Depends(database_connection),
):
    require_member(conn, member_id)
    report = conn.execute(
        "SELECT id, member_id, title, report_date, report_type, source_type, status, raw_text "
        "FROM reports WHERE id = %s AND member_id = %s AND status = 'confirmed'",
        (report_id, member_id),
    ).fetchone()
    if report is None:
        raise HTTPException(status_code=404, detail="该成员下没有这份已确认报告。")
    return report


@router.get("/metrics")
def list_metrics(
    member_id: str = Query(min_length=1, max_length=80),
    metric_code: str | None = Query(default=None, min_length=1, max_length=80),
    conn=Depends(database_connection),
):
    member = require_member(conn, member_id)
    sql = (
        "SELECT m.id, m.report_id, r.member_id, r.report_date, m.metric_code, m.metric_name, "
        "m.value::text AS value, m.unit, m.reference_range, m.source_text "
        "FROM metrics m JOIN reports r ON r.id = m.report_id "
        "WHERE r.member_id = %s AND r.status = 'confirmed' AND m.confirmed = TRUE"
    )
    params = [member_id]
    if metric_code is not None:
        sql += " AND m.metric_code = %s"
        params.append(metric_code)
    sql += " ORDER BY r.report_date, r.id, m.metric_code"
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        row["source_id"] = f"metric:{row['id']}"
    # JSON 中用字符串保留十进制数值；计算时再转 Decimal，不做浮点比较。
    return {"member": member, "items": rows, "count": len(rows)}