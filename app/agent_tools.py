"""M4 的三个只读工具；范围来自本次请求，模型只能缩小范围。"""

from datetime import date
from decimal import Decimal
from typing import Annotated

import psycopg
from fastapi import HTTPException
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.db import connect_db
from app.retrieval import retrieve

ID = Annotated[str, Field(min_length=1, max_length=160)]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    member_id: str = Field(min_length=1, max_length=80)
    report_ids: list[ID] | None = Field(default=None, min_length=1, max_length=40)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def date_order(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date 不能晚于 end_date。")
        return self


class ReportInput(ToolInput):
    report_type: str | None = Field(default=None, min_length=1, max_length=80)


class SearchInput(ToolInput):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=2, ge=1, le=8)


class TrendInput(ToolInput):
    metric_code: str = Field(min_length=1, max_length=80)
    latest_n: int | None = Field(default=None, ge=1, le=40)


INPUTS = {"get_report_list": ReportInput, "search_reports": SearchInput,
          "get_metric_trend": TrendInput}
DESCRIPTIONS = {
    "get_report_list": "列出指定成员范围内已确认的报告，可按日期和类型筛选。只返回报告元数据，不含正文。",
    "search_reports": "检索指定成员的报告原文，回答结论、备注、建议。跨报告对比时分别指定各报告查询。top_k 默认2。",
    "get_metric_trend": "查询已确认的指标数值。metric_code 使用上下文的指标目录；latest_n=1查询最新一条，2查询最近两条，不传查询范围内全部。返回日期、单位、来源，并由代码计算同单位记录的变化。",
}
TOOLS = [{"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name],
          "parameters": schema.model_json_schema()}} for name, schema in INPUTS.items()]


class ToolFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def prepare_scope(member_ids, report_ids=None):
    """本次查询范围快照。报告总数上限40；不是多用户权限系统。"""
    with connect_db() as conn:
        conn.row_factory = dict_row
        members = conn.execute("SELECT id, name, aliases FROM family_members "
                               "WHERE id=ANY(%s::text[]) ORDER BY id", (member_ids,)).fetchall()
        if {r["id"] for r in members} != set(member_ids):
            raise HTTPException(404, "指定成员不存在。")
        sql = ("SELECT id, member_id, title, report_date, report_type FROM reports "
               "WHERE member_id=ANY(%s::text[]) AND status='confirmed'")
        params = [member_ids]
        if report_ids is not None:
            sql += " AND id=ANY(%s::text[])"
            params.append(report_ids)
        reports = conn.execute(sql + " ORDER BY report_date, id LIMIT 41", params).fetchall()
        if len(reports) > 40:
            raise HTTPException(422, "本次范围超过40份报告，请通过 report_ids 缩小范围。")
        if report_ids is not None and {r["id"] for r in reports} != set(report_ids):
            raise HTTPException(404, "指定范围包含不属于所选成员的报告或未确认报告。")
        ids = [r["id"] for r in reports]
        catalog = conn.execute(
            "SELECT DISTINCT m.metric_code, m.metric_name FROM metrics m "
            "JOIN reports r ON r.id=m.report_id WHERE r.member_id=ANY(%s::text[]) "
            "AND r.id=ANY(%s::text[]) AND r.status='confirmed' AND m.confirmed=TRUE "
            "ORDER BY m.metric_code, m.metric_name LIMIT 100", (member_ids, ids),
        ).fetchall()
    for row in reports:
        row["report_date"] = row["report_date"].isoformat()
    return {"members": members, "reports": reports, "metric_catalog": catalog,
            "reports_by_member": {m: [r["id"] for r in reports if r["member_id"] == m]
                                  for m in member_ids}}


def constrained_ids(args, scope):
    if args.member_id not in scope["reports_by_member"]:
        raise ToolFailure("SCOPE_VIOLATION", "该成员不在本次请求范围内，不能扩大范围。")
    allowed = scope["reports_by_member"][args.member_id]
    ids = list(dict.fromkeys(args.report_ids)) if args.report_ids is not None else allowed
    if not set(ids).issubset(allowed):
        raise ToolFailure("SCOPE_VIOLATION", "报告不属于该成员或不在本次请求范围内。")
    return ids


def scoped_where(args, ids):
    sql = "r.member_id=%s AND r.status='confirmed' AND r.id=ANY(%s::text[])"
    params = [args.member_id, ids]
    if args.start_date is not None:
        sql += " AND r.report_date >= %s"
        params.append(args.start_date)
    if args.end_date is not None:
        sql += " AND r.report_date <= %s"
        params.append(args.end_date)
    return sql, params


def get_report_list(args, ids):
    where, params = scoped_where(args, ids)
    if args.report_type is not None:
        where += " AND r.report_type=%s"
        params.append(args.report_type)
    with connect_db() as conn:
        conn.row_factory = dict_row
        rows = conn.execute("SELECT r.id AS report_id, r.member_id, r.title, "
                            "r.report_date, r.report_type FROM reports r WHERE " + where +
                            " ORDER BY r.report_date, r.id", params).fetchall()
    for row in rows:
        row["report_date"] = row["report_date"].isoformat()
        row["source_id"] = "report:" + row["report_id"]
        row["content"] = f"报告：{row['title']}；日期：{row['report_date']}；类型：{row['report_type']}。"
    return {"status": "ok" if rows else "empty", "items": rows}


def get_metric_trend(args, ids):
    where, params = scoped_where(args, ids)
    with connect_db() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            "SELECT m.id, m.report_id, r.member_id, r.title, r.report_date, "
            "m.metric_code, m.metric_name, m.value::text AS value, m.unit, "
            "m.reference_range, m.source_text FROM metrics m JOIN reports r ON r.id=m.report_id "
            "WHERE " + where + " AND m.confirmed=TRUE AND m.metric_code=%s "
            "ORDER BY r.report_date DESC, r.id DESC", [*params, args.metric_code],
        ).fetchall()
    if args.latest_n and len(rows) > args.latest_n:
        if rows[args.latest_n - 1]["report_date"] == rows[args.latest_n]["report_date"]:
            raise ToolFailure("AMBIGUOUS_DATE", "最新记录的截止日期存在多份报告，请明确 report_ids。")
        rows = rows[:args.latest_n]
    rows.reverse()
    for row in rows:
        row["source_id"] = f"metric:{row['id']}"
        row["report_date"] = row["report_date"].isoformat()
    comparison = {"comparable": False, "delta": None, "reason": "少于两条记录。"}
    if len(rows) >= 2:
        if len({r["unit"] for r in rows}) != 1:
            comparison["reason"] = "单位不一致，本版未实现单位换算。"
        elif rows[0]["report_date"] == rows[-1]["report_date"] or any(
            sum(r["report_date"] == endpoint for r in rows) > 1
            for endpoint in (rows[0]["report_date"], rows[-1]["report_date"])
        ):
            comparison["reason"] = "端点日期存在多份报告，无法确定时间先后。"
        else:
            comparison = {"comparable": True,
                          "delta": str(Decimal(rows[-1]["value"]) - Decimal(rows[0]["value"])),
                          "unit": rows[0]["unit"], "from_source_id": rows[0]["source_id"],
                          "to_source_id": rows[-1]["source_id"], "reason": "末次减首次，由 Decimal 计算。"}
    return {"status": "ok" if rows else "empty", "items": rows, "comparison": comparison}


def execute_tool(name, raw_arguments, scope, chunk_version, retrieval_mode):
    """仅从白名单分派；返回错误码，不把异常包装成空数据。"""
    trace, usage = [], {}
    try:
        if name not in INPUTS:
            raise ToolFailure("UNKNOWN_TOOL", "工具不存在，请从三个已定义工具中选择。")
        # JSON 严格校验仍允许标准 ISO 日期，不把 '2'、true 隐式转换成整数。
        args = INPUTS[name].model_validate_json(raw_arguments)
        ids = constrained_ids(args, scope)
        trace.append({"step": "tool_scope", "member_id": args.member_id, "report_ids": ids,
                      "validated_arguments": args.model_dump(mode="json")})
        if name == "get_report_list":
            result = get_report_list(args, ids)
        elif name == "get_metric_trend":
            result = get_metric_trend(args, ids)
        else:
            # 先在数据库按日期缩小范围，再把明确报告 ID 交给 M3 的两条检索通道。
            reports = get_report_list(ReportInput.model_validate_json(
                args.model_dump_json(exclude={"query", "top_k"})), ids)["items"]
            selected = [r["report_id"] for r in reports]
            rows = retrieve(args.query, args.member_id, selected, args.top_k, trace, usage,
                            chunk_version=chunk_version, retrieval_mode=retrieval_mode) if selected else []
            result = {"status": "ok" if rows else "empty", "items": rows}
    except ValidationError as exc:
        result = {"status": "error", "code": "INVALID_ARGUMENT", "message": "工具参数格式错误。",
                  "fields": [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]}
                             for e in exc.errors()]}
    except ToolFailure as exc:
        result = {"status": "error", "code": exc.code, "message": exc.message}
    except APITimeoutError:
        result = {"status": "error", "code": "EMBEDDING_TIMEOUT", "message": "检索所需的向量接口超时。"}
    except APIStatusError as exc:
        result = {"status": "error", "code": "EMBEDDING_HTTP_ERROR",
                  "message": "向量服务返回错误。", "upstream_status": exc.status_code}
    except APIConnectionError:
        result = {"status": "error", "code": "EMBEDDING_CONNECTION_ERROR", "message": "无法连接向量服务。"}
    except psycopg.Error:
        result = {"status": "error", "code": "DATABASE_ERROR", "message": "工具读取数据库失败。"}
    except HTTPException as exc:
        result = {"status": "error", "code": f"TOOL_HTTP_{exc.status_code}", "message": str(exc.detail)}
    except (APIError, ValueError, KeyError, TypeError):
        result = {"status": "error", "code": "TOOL_RESPONSE_ERROR", "message": "工具配置或返回格式异常。"}
    return result, trace, usage