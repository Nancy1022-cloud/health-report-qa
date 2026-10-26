"""复用 M2 原文标注，交错运行 A/B/C；默认只评检索，保留失败类型。"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from psycopg.rows import dict_row

from app.db import connect_db
from app.eval_baseline import evidence_recall, locate_gold
from app.rag import embedding_config, run_query
from app.rrf import CANDIDATE_K, RRF_K
from app.text_search import SEARCH_VERSION

APP_DIR = Path(__file__).resolve().parent
CONFIGS = [
    {"name": "A_fixed_vector", "chunk_version": "fixed200o40", "retrieval_mode": "vector"},
    {"name": "B_section_vector", "chunk_version": "section200o40v1", "retrieval_mode": "vector"},
    {"name": "C_section_hybrid", "chunk_version": "section200o40v1", "retrieval_mode": "hybrid"},
]


def load_reports():
    with connect_db() as conn:
        conn.row_factory = dict_row
        return {row["id"]: row for row in conn.execute(
            "SELECT id, member_id, raw_text FROM reports WHERE status='confirmed' ORDER BY id"
        ).fetchall()}


def fingerprint(value):
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def read_log(query_id):
    if not query_id:
        return None
    with connect_db() as conn:
        conn.row_factory = dict_row
        return conn.execute("SELECT trace, latency_ms, token_usage FROM query_logs WHERE id=%s", (query_id,)).fetchone()


def summarize(items, with_answers):
    answerable = [item for item in items if item["expected_answerable"]]
    retrieved = [item for item in items if item["retrieval_ok"]]
    scored = [item for item in answerable if item["retrieval_ok"]]
    full = sum(item["all_evidence_covered"] is True for item in answerable)
    summary = {
        "cases": len(items), "retrieval_succeeded": len(retrieved),
        "retrieval_errors": len(items) - len(retrieved),
        "query_errors": sum(not item["query_ok"] for item in items),
        "answerable_cases": len(answerable), "all_evidence_covered": f"{full}/{len(answerable)}",
        "mean_evidence_recall_at_k": sum(item["evidence_recall_at_k"] or 0 for item in answerable) / len(answerable) if answerable else None,
        "mean_recall_successful_retrieval": sum(item["evidence_recall_at_k"] for item in scored) / len(scored) if scored else None,
        "scope_violations": sum(item["scope_violations"] for item in retrieved),
        "mean_retrieved_characters": round(sum(item["retrieved_characters"] for item in retrieved) / len(retrieved)) if retrieved else None,
        "mean_latency_ms_successful_query": round(sum(item["result"]["latency_ms"] for item in items if item["query_ok"]) /
                                                  sum(item["query_ok"] for item in items)) if any(item["query_ok"] for item in items) else None,
        "manual_answer_correct": None,
    }
    summary["model_calls_returned_by_successful_queries"] = sum(item["result"]["model_calls"] for item in items if item["query_ok"])
    summary["model_calls_observed_in_logs"] = sum(event["step"] in {"embedding_request", "generation_request"}
                                                   for item in items for event in item.get("trace", []))
    if with_answers:
        summary["generation_errors_after_retrieval"] = sum(item["retrieval_ok"] and not item["query_ok"] for item in items)
        summary["answerable_agreement"] = f"{sum(item.get('answerable_agreement') is True for item in items)}/{len(items)}"
    return summary


def evaluate_case(case, spans, config, args):
    item = {"case_id": case["id"], "question": case["question"], "expected_answerable": case["answerable"],
            "reference_answer": case["reference_answer"], "manual_answer_correct": None,
            "query_ok": False, "retrieval_ok": False, "scope_violations": 0,
            "evidence_recall_at_k": None, "all_evidence_covered": None}
    evidence, query_id = [], None
    try:
        result = run_query(case["question"], case["member_id"], case["report_ids"], args.top_k,
                           with_answer=args.with_answers, chunk_version=config["chunk_version"],
                           retrieval_mode=config["retrieval_mode"])
        item.update(query_ok=True, retrieval_ok=True, result=result)
        evidence, query_id = result["evidence"], result["query_id"]
        if args.with_answers:
            item["answerable_agreement"] = (result["status"] == "answered") == case["answerable"]
    except HTTPException as exc:
        item.update(http_status=exc.status_code, error=exc.detail)
        if isinstance(exc.detail, dict):
            query_id = exc.detail.get("query_id")
        if args.with_answers:
            item["answerable_agreement"] = False
    log = read_log(query_id)
    if log:
        item["trace"] = log["trace"]
        item["observed_latency_ms"] = log["latency_ms"]
        item["observed_token_usage"] = log["token_usage"]
        retrieval_events = [event for event in log["trace"] if event["step"] == "retrieval"]
        # 生成失败时，使用实际已完成的检索证据，不把它记成召回为零。
        if retrieval_events:
            evidence = retrieval_events[-1]["evidence"]
            item["retrieval_ok"] = True
    if item["retrieval_ok"]:
        item["evidence_recall_at_k"] = evidence_recall(spans, evidence)
        item["scope_violations"] = sum(row["member_id"] != case["member_id"] or (
            case["report_ids"] is not None and row["report_id"] not in case["report_ids"]
        ) or row["chunk_version"] != config["chunk_version"] for row in evidence)
    elif spans:
        item["evidence_recall_at_k"] = 0.0
    if spans:
        item["all_evidence_covered"] = item["evidence_recall_at_k"] == 1.0
    item["evidence"] = evidence
    item["retrieved_characters"] = sum(len(row["content"]) for row in evidence)
    return item


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=APP_DIR / "data" / "eval_m2.json")
    parser.add_argument("--top-k", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--with-answers", action="store_true")
    args = parser.parse_args()
    dataset_text = args.dataset.read_text(encoding="utf-8-sig")
    cases = json.loads(dataset_text)["cases"]
    if not cases or len({case["id"] for case in cases}) != len(cases):
        raise ValueError("问题集为空或题号重复。")
    reports = load_reports()
    spans = {case["id"]: locate_gold(case, reports) for case in cases}
    for case in cases:
        if bool(case["gold"]) != case["answerable"]:
            raise ValueError("有答案题必须有原文证据，无答案题的 gold 应为空。")
    grouped = {config["name"]: [] for config in CONFIGS}
    for case in cases:
        # 每道题交错运行三种配置，降低不同时间段服务负载造成的偏差。
        for config in CONFIGS:
            item = evaluate_case(case, spans[case["id"]], config, args)
            grouped[config["name"]].append(item)
            print(f"{config['name']} {case['id']}: retrieval_ok={item['retrieval_ok']}, recall={item['evidence_recall_at_k']}", flush=True)
    model, dimensions = embedding_config()
    stable = fingerprint(load_reports()) == fingerprint(reports) and args.dataset.read_text(encoding="utf-8-sig") == dataset_text
    comparisons = [{**config, "summary": summarize(grouped[config["name"]], args.with_answers),
                    "items": grouped[config["name"]]} for config in CONFIGS]
    output = {"stage": "M3", "valid_comparison": stable, "top_k": args.top_k,
              "with_answers": args.with_answers, "embedding_model": model, "dimensions": dimensions,
              "llm_model": os.getenv("LLM_MODEL"), "candidate_k": CANDIDATE_K, "rrf_k": RRF_K,
              "search_version": SEARCH_VERSION,
              "dataset_sha256": hashlib.sha256(dataset_text.encode()).hexdigest(),
              "reports_sha256": fingerprint(reports), "comparisons": comparisons}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    suffix = "answers" if args.with_answers else "retrieval"
    path = APP_DIR / "data" / f"m3_compare_{suffix}_{stamp}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    table = ["# M3 实测对比", "", f"可比较：{stable}；top_k={args.top_k}；生成回答：{args.with_answers}", "",
             "| 配置 | 完整证据覆盖（有答案题） | 平均证据召回 | 检索故障 | 全链路故障 | 范围违规片段 | 成功请求平均耗时 ms |",
             "|---|---|---|---|---|---|---|"]
    for comparison in comparisons:
        s = comparison["summary"]
        mean = s["mean_evidence_recall_at_k"]
        table.append(f"| {comparison['name']} | {s['all_evidence_covered']} | {mean if mean is not None else 'N/A'} | "
                     f"{s['retrieval_errors']} | {s['query_errors']} | {s['scope_violations']} | {s['mean_latency_ms_successful_query']} |")
    table.extend(["", "这些题是开发冒烟集。没有运行生成时，无答案拒答、答案及引用正确性尚未验收。",
                  "完整覆盖按原文字符区间计分，不等同于语义正确；答案仍需人工核对。",
                  "A→B 只变切分；B→C 只变检索。失败单独统计，不用补测结果覆盖旧文件。", ""])
    path.with_suffix(".md").write_text("\n".join(table), encoding="utf-8")
    print(json.dumps({"valid_comparison": stable, "saved_to": str(path),
                      "table": str(path.with_suffix('.md')),
                      "summary": {c["name"]: c["summary"] for c in comparisons}}, ensure_ascii=False, indent=2))
    if not stable:
        raise SystemExit("评测期间报告或题集发生变化，本轮结果不可用于对比。")


if __name__ == "__main__":
    main()