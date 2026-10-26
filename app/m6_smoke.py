"""在用户本机验收合成报告：真实数据库、真实关键词检索，不额外调用模型。"""

import argparse
import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException
from psycopg.rows import dict_row

from app.agent_tools import execute_tool, prepare_scope
from app.chunking import VERSIONS
from app.db import connect_db
from app.rag import embedding_config
from app.report_ingest import get_draft
from app.text_search import SEARCH_VERSION


def main():
    parser = argparse.ArgumentParser(description="仅用于 samples/m6_mom_synthetic.pdf 或同内容图片。")
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--member-id", default="member_mom")
    parser.add_argument("--expect-draft", action="store_true", help="确认前检查：草稿不可进入工具范围，业务表没有该报告。")
    args = parser.parse_args()
    draft = get_draft(args.report_id, args.member_id)
    checks = []

    def check(name, passed, observed):
        checks.append({"check": name, "passed": bool(passed), "observed": observed})

    if args.expect_draft:
        check("report_stays_draft", draft["status"] == "draft", draft["status"])
        with connect_db() as conn:
            counts = [conn.execute(f"SELECT count(*) FROM {table} WHERE {column}=%s", (args.report_id,)).fetchone()[0]
                      for table, column in [("reports", "id"), ("metrics", "report_id"), ("report_chunks", "report_id")]]
        check("no_business_rows_before_confirmation", counts == [0, 0, 0], counts)
        rejected = False
        try:
            prepare_scope([args.member_id], [args.report_id])
        except HTTPException as exc:
            rejected = exc.status_code == 404
        check("draft_not_queryable", rejected, "404" if rejected else "未拦截")
        print(json.dumps({"stage": "M6_draft", "checks": checks}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if all(c["passed"] for c in checks) else 1)

    check("report_confirmed", draft["status"] == "confirmed", draft["status"])
    scope = prepare_scope([args.member_id], [args.report_id])
    model, dimensions = embedding_config()
    digest = hashlib.sha256(draft["raw_text"].encode()).hexdigest()
    with connect_db() as conn:
        conn.row_factory = dict_row
        chunks = conn.execute("SELECT chunk_version, content, start_char, end_char, source_sha256, "
                              "embedding_model, embedding_dimensions, search_version, "
                              "embedding IS NOT NULL AS has_vector, search_vector IS NOT NULL AS has_keywords "
                              "FROM report_chunks WHERE report_id=%s", (args.report_id,)).fetchall()
    check("both_index_versions", {c["chunk_version"] for c in chunks} == set(VERSIONS.values()),
          sorted({c["chunk_version"] for c in chunks}))
    check("index_config_and_source", bool(chunks) and all(c["source_sha256"] == digest and
          c["content"] == draft["raw_text"][c["start_char"]:c["end_char"]] and c["embedding_model"] == model and
          c["embedding_dimensions"] == dimensions and c["search_version"] == SEARCH_VERSION and
          c["has_vector"] and c["has_keywords"] for c in chunks), len(chunks))
    for code, expected in [("fasting_glucose", "5.70"), ("total_cholesterol", "4.80"), ("triglycerides", "1.10")]:
        result, _, _ = execute_tool("get_metric_trend", json.dumps({"member_id": args.member_id,
            "report_ids": [args.report_id], "metric_code": code}), scope, "section200o40v1", "hybrid")
        items = result.get("items", [])
        check(code, result.get("status") == "ok" and len(items) == 1 and
              Decimal(items[0]["value"]) == Decimal(expected) and items[0]["unit"] == "mmol/L" and
              items[0]["report_date"] == "2026-10-20" and items[0]["member_id"] == args.member_id,
              result)
    result, _, _ = execute_tool("search_reports", json.dumps({"member_id": args.member_id,
        "report_ids": [args.report_id], "query": "复查 建议", "top_k": 4}), scope, "section200o40v1", "keyword")
    check("real_keyword_retrieval", result.get("status") == "ok" and
          any("复查" in row["content"] for row in result.get("items", [])), result)
    with connect_db() as conn:
        other = conn.execute("SELECT id FROM family_members WHERE id<>%s ORDER BY id LIMIT 1", (args.member_id,)).fetchone()
    isolated = False
    if other:
        try:
            prepare_scope([other[0]], [args.report_id])
        except HTTPException as exc:
            isolated = exc.status_code == 404
    check("wrong_member_rejected", isolated, "404" if isolated else "未通过或没有第二位成员")
    print(json.dumps({"stage": "M6", "checks": checks, "passed": sum(c["passed"] for c in checks),
                      "total": len(checks)}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if all(c["passed"] for c in checks) else 1)


if __name__ == "__main__":
    main()