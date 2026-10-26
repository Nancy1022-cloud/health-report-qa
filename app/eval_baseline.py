"""M2 开发基线：默认只评测召回；--with-answers 额外调用生成模型。"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from psycopg.rows import dict_row

from app.db import connect_db
from app.rag import CHUNK_VERSION, DEFAULT_TOP_K, embedding_config, run_query

APP_DIR = Path(__file__).resolve().parent


def locate_gold(case, reports):
    spans = []
    for gold in case["gold"]:
        report = reports[gold["report_id"]]
        if report["member_id"] != case["member_id"]:
            raise ValueError(f"{case['id']} 的标注成员不匹配。")
        text, target = report["raw_text"], gold["text"]
        start = text.find(target)
        if not target or start < 0 or text.find(target, start + 1) >= 0:
            raise ValueError(f"{case['id']} 的证据必须在原文中唯一出现。")
        spans.append({"report_id": gold["report_id"], "start": start, "end": start + len(target)})
    return spans


def evidence_recall(spans, evidence):
    """合并片段字符区间，避免重叠片段重复计分，也允许证据横跨切分边界。"""
    covered = 0
    for span in spans:
        cursor = span["start"]
        intervals = sorted((item["start_char"], item["end_char"])
                           for item in evidence if item["report_id"] == span["report_id"])
        for start, end in intervals:
            if start <= cursor:
                cursor = max(cursor, end)
        covered += int(cursor >= span["end"])
    return covered / len(spans) if spans else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-answers", action="store_true")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, choices=range(1, 9))
    args = parser.parse_args()
    dataset_path = APP_DIR / "data" / "eval_m2.json"
    dataset_text = dataset_path.read_text(encoding="utf-8-sig")
    cases = json.loads(dataset_text)["cases"]
    with connect_db() as conn:
        conn.row_factory = dict_row
        reports = {row["id"]: row for row in conn.execute(
            "SELECT id, member_id, raw_text FROM reports WHERE status = 'confirmed' ORDER BY id",
        ).fetchall()}
    # 开始调用模型前先检查所有标注，避免标注有误却浪费模型请求。
    gold_spans = {case["id"]: locate_gold(case, reports) for case in cases}
    results = []
    for case in cases:
        item = {"case_id": case["id"], "question": case["question"],
                "expected_answerable": case["answerable"], "reference_answer": case["reference_answer"]}
        try:
            result = run_query(case["question"], case["member_id"], case["report_ids"],
                               args.top_k, with_answer=args.with_answers)
            spans = gold_spans[case["id"]]
            recall = evidence_recall(spans, result["evidence"])
            violations = sum(item["member_id"] != case["member_id"] or (
                case["report_ids"] is not None and item["report_id"] not in case["report_ids"]
            ) for item in result["evidence"])
            item.update({"ok": True, "evidence_recall_at_k": recall,
                         "all_evidence_covered": recall == 1.0 if spans else None,
                         "scope_violations": violations, "result": result,
                         "manual_answer_correct": None})
            if args.with_answers:
                item["answerable_agreement"] = (result["status"] == "answered") == case["answerable"]
        except HTTPException as exc:
            # 故障保留为故障；不算成成功拒答，也不从评测分母中静默排除。
            item.update({"ok": False, "http_status": exc.status_code, "error": exc.detail,
                         "evidence_recall_at_k": 0.0 if case["gold"] else None,
                         "all_evidence_covered": False if case["gold"] else None,
                         "answerable_agreement": False if args.with_answers else None})
        results.append(item)
        print(f"{case['id']}: ok={item['ok']}, evidence_recall={item['evidence_recall_at_k']}", flush=True)

    answerable = [item for item in results if item["expected_answerable"]]
    succeeded = [item for item in results if item["ok"]]
    full = sum(item["all_evidence_covered"] is True for item in answerable)
    summary = {
        "cases": len(results), "succeeded": len(succeeded), "errors": len(results) - len(succeeded),
        "answerable_cases": len(answerable),
        "mean_evidence_recall_at_k": sum(item["evidence_recall_at_k"] for item in answerable) / len(answerable),
        "all_evidence_covered": f"{full}/{len(answerable)}",
        "scope_violations": sum(item["scope_violations"] for item in succeeded),
        "mean_latency_ms_successful": round(sum(item["result"]["latency_ms"] for item in succeeded) / len(succeeded)) if succeeded else None,
    }
    if args.with_answers:
        agreements = sum(item.get("answerable_agreement") is True for item in results)
        summary["answerable_agreement"] = f"{agreements}/{len(results)}"
    source_text = json.dumps(reports, sort_keys=True, ensure_ascii=False)
    model, dimensions = embedding_config()
    output = {"stage": "M2", "chunk_version": CHUNK_VERSION, "top_k": args.top_k,
              "with_answers": args.with_answers, "embedding_model": model, "dimensions": dimensions,
              "dataset_sha256": hashlib.sha256(dataset_text.encode()).hexdigest(),
              "reports_sha256": hashlib.sha256(source_text.encode()).hexdigest(),
              "summary": summary, "items": results}
    # app/data 有 Compose 挂载，结果会出现在你电脑上的同一文件夹。
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    suffix = "answers" if args.with_answers else "retrieval"
    path = APP_DIR / "data" / f"m2_baseline_{suffix}_{stamp}.json"
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": summary, "saved_to": str(path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()