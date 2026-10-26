"""真实调用本机API并保存工具轨迹；不是模型答案的自动满分评测。"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CASES = [
    {"id": "list", "question": "列出爸爸现有报告的日期和报告类型。", "member_ids": ["member_dad"],
     "expected_tools": ["get_report_list"], "expected": "2026-01-10体检报告；2026-07-10复查报告。"},
    {"id": "latest", "question": "爸爸最近一次空腹血糖是多少？", "member_ids": ["member_dad"],
     "expected_tools": ["get_metric_trend"], "expected": "2026-07-10；5.80 mmol/L；latest_n=1。"},
    {"id": "report", "question": "妈妈2026年7月12日这份报告的检查结论是什么？", "member_ids": ["member_mom"],
     "expected_tools": ["search_reports"], "expected": "总胆固醇超过报告参考上限；另两项在报告参考范围。"},
    {"id": "combined", "question": "对比爸爸2026年1月10日和7月10日的空腹血糖变化，并分别列出两份报告的复查建议。", "member_ids": ["member_dad"],
     "expected_tools": ["get_metric_trend", "search_reports"],
     "expected": "6.40到5.80 mmol/L，变化-0.60；两份报告建议均有对应原文引用。"},
    {"id": "cross_member", "question": "分别查询爸爸和妈妈最近一次空腹血糖，标明各自日期和来源。", "member_ids": ["member_dad", "member_mom"],
     "expected_tools": ["get_metric_trend"], "expected": "爸爸2026-07-10为5.80；妈妈2026-07-12为5.20；分别引用。"},
    {"id": "missing", "question": "爸爸2026年7月10日的尿酸具体数值是多少？", "member_ids": ["member_dad"],
     "expected_tools": [], "expected": "insufficient；不能编造尿酸数值，也不能把工具报错说成无记录。"},
    {"id": "clarify", "question": "最近一次空腹血糖是多少？", "member_ids": [],
     "expected_tools": [], "expected": "clarify；不调用模型和工具，要求先选择成员。"},
]


def request_json(base, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urlopen(req, timeout=145) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--case", choices=[c["id"] for c in CASES])
    args = parser.parse_args()
    rows = []
    for case in CASES:
        if args.case and case["id"] != args.case:
            continue
        body = {k: case[k] for k in ("question", "member_ids")}
        body.update(chunk_version="section200o40v1", retrieval_mode="hybrid")
        try:
            http_status, response = request_json(args.base_url, "/api/agent/query", body)
            detail = response.get("detail")
            query_id = response.get("query_id") or (detail.get("query_id") if isinstance(detail, dict) else None)
            trace_status, log = request_json(args.base_url, f"/api/query/{query_id}/trace") if query_id else (None, {})
            observed = [e["name"] for e in log.get("trace", []) if e["step"] == "tool_request"]
            rows.append({"case": case, "http_status": http_status, "response": response,
                         "trace_status": trace_status, "log": log, "observed_tools": observed,
                         "expected_tools_observed": set(case["expected_tools"]).issubset(observed),
                         "manual_answer_correct": None, "manual_parameter_correct": None})
            print(json.dumps({"id": case["id"], "http_status": http_status,
                              "status": response.get("status"), "tools": observed,
                              "manual_review_required": True}, ensure_ascii=False), flush=True)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            rows.append({"case": case, "error_type": type(exc).__name__, "manual_answer_correct": None})
            print(json.dumps({"id": case["id"], "error_type": type(exc).__name__}, ensure_ascii=False), flush=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = Path(__file__).parent / "data" / f"m4_smoke_{stamp}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"stage": "M4", "results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果已保存：" + str(output), flush=True)
    if any(r.get("http_status") != 200 or r.get("trace_status") != 200 for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()