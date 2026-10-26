"""M5 真实API冒烟验证：保存证据检查与调用预算；答案与参数仍需人工核对。"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CASES = [
    {"id": "latest", "question": "爸爸最近一次空腹血糖是多少？", "member_ids": ["member_dad"],
     "expected": "2026-07-10，5.80 mmol/L，实际metric引用。可走structured快路径，无需补检索。"},
    {"id": "report", "question": "妈妈2026年7月12日这份报告的检查结论是什么？", "member_ids": ["member_mom"],
     "expected": "实际chunk支持检查结论；来源属于妈妈这份报告，检查不能只用report目录。"},
    {"id": "combined", "question": "对比爸爸2026年1月10日和7月10日的空腹血糖变化，并分别列出两份报告的复查建议。",
     "member_ids": ["member_dad"],
     "expected": "6.40→5.80 mmol/L，代码差值-0.60；两份建议均有实际原文。样例有答案，正常应答全；partial只说明结束机制有效，答案项仍未通过。"},
    {"id": "missing", "question": "爸爸2026年7月10日的尿酸具体数值是多少？", "member_ids": ["member_dad"],
     "expected": "insufficient，不编造尿酸值。已确认指标未查到不应对报告原文反复补检索；工具失败应明确返回错误。"},
    {"id": "cross_member", "question": "分别查询爸爸和妈妈最近一次空腹血糖，标明各自日期和来源。",
     "member_ids": ["member_dad", "member_mom"],
     "expected": "爸爸2026-07-10为5.80，妈妈2026-07-12为5.20；分别有metric来源，不推断谁更健康。"},
    {"id": "clarify", "question": "最近一次空腹血糖是多少？", "member_ids": [],
     "expected": "clarify，模型、工具调用均为0，提示选择成员。"},
]


def request_json(base, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = Request(base + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urlopen(req, timeout=145) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def observed_checks(response, log):
    trace = log.get("trace", [])
    tool_requests = [e for e in trace if e["step"] == "tool_request"]
    model_requests = [e for e in trace if e["step"] == "agent_model_request"]
    supplements = [e for e in tool_requests if e.get("origin") == "evidence_controller"]
    revisions = [e for e in model_requests if e.get("stage") == "answer_revision"]
    reviews = [e for e in model_requests if e.get("stage") == "evidence_review"]
    available = {r["source_id"] for r in response.get("evidence", [])}
    used = {r["source_id"] for r in response.get("citations", [])}
    checks = {"tool_calls_at_most_6": len(tool_requests) <= 6,
              "model_calls_at_most_10": len(model_requests) <= 10,
              "supplement_at_most_1": len(supplements) <= 1,
              "revision_at_most_1": len(revisions) <= 1,
              "reviews_at_most_2": len(reviews) <= 2,
              "citations_are_actual_evidence": used.issubset(available),
              "controller_search_is_one_report": all(
                  e["name"] == "search_reports" and len(json.loads(e["arguments"])["report_ids"]) == 1
                  for e in supplements)}
    if response.get("query_id"):
        checks.update(tool_count_matches_log=response["tool_calls"] == len(tool_requests),
                      model_count_matches_log=response["llm_calls"] == len(model_requests),
                      supplement_count_matches_log=response["supplemental_calls"] == len(supplements),
                      revision_count_matches_log=response["revision_calls"] == len(revisions))
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--case", choices=[c["id"] for c in CASES])
    args = parser.parse_args()
    rows = []
    for case in CASES:
        if args.case and args.case != case["id"]:
            continue
        body = {k: case[k] for k in ("question", "member_ids")}
        body.update(chunk_version="section200o40v1", retrieval_mode="hybrid")
        try:
            http_status, response = request_json(args.base_url, "/api/agent/query", body)
            detail = response.get("detail")
            query_id = response.get("query_id") or (detail.get("query_id") if isinstance(detail, dict) else None)
            trace_status, log = request_json(args.base_url, f"/api/query/{query_id}/trace") if query_id else (None, {})
            checks = observed_checks(response, log) if trace_status == 200 else {}
            evidence_check = response.get("evidence_check", {})
            rows.append({"case": case, "http_status": http_status, "response": response,
                         "trace_status": trace_status, "log": log, "automatic_checks": checks,
                         "manual_answer_correct": None, "manual_parameters_correct": None,
                         "manual_need_list_complete": None, "manual_citation_support_correct": None})
            print(json.dumps({"id": case["id"], "http_status": http_status,
                              "status": response.get("status"), "tool_calls": response.get("tool_calls"),
                              "llm_calls": response.get("llm_calls"), "supplemental_calls": response.get("supplemental_calls"),
                              "revision_calls": response.get("revision_calls"),
                              "answer_mode": evidence_check.get("answer_mode"),
                              "missing_needs": [n["topic"] for n in evidence_check.get("coverage", []) if not n["covered"]],
                              "automatic_checks_passed": bool(checks) and all(checks.values()),
                              "manual_review_required": True}, ensure_ascii=False), flush=True)
        except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            rows.append({"case": case, "error_type": type(exc).__name__, "manual_answer_correct": None})
            print(json.dumps({"id": case["id"], "error_type": type(exc).__name__}, ensure_ascii=False), flush=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output = Path(__file__).parent / "data" / f"m5_smoke_{stamp}.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"stage": "M5", "results": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("结果已保存：" + str(output), flush=True)
    if any(r.get("http_status") != 200 or r.get("trace_status") != 200 or not r.get("automatic_checks")
           or not all(r["automatic_checks"].values()) for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()