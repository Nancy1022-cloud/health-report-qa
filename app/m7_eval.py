"""调用真实HTTP接口，保留逐题结果；自动检查不代替人工语义验收。"""

import argparse
import json
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from app.m7_data import APP_DIR, build_bundle, digest, prepare, snapshot

MANUAL_FIELDS = ["answer_correct", "citation_support", "tool_parameters_correct"]


def request_json(base, path, body=None, timeout=150):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = Request(base.rstrip("/") + path, data=data,
                      headers={"Content-Type": "application/json; charset=utf-8"},
                      method="GET" if body is None else "POST")
    # 容器内直接访问本机服务，不把localhost请求交给代理。
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            code, raw = response.status, response.read()
    except HTTPError as exc:
        code, raw = exc.code, exc.read()
    except (URLError, TimeoutError, OSError) as exc:
        return 0, {"transport_error": type(exc).__name__, "message": str(exc)[:400]}
    try:
        return code, json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return code, {"invalid_json": raw.decode("utf-8", errors="replace")[:400]}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def source_valid(item, reports):
    try:
        report = reports[item["report_id"]]
        if item["member_id"] != report["member_id"]:
            return False
        sid = item["source_id"]
        if sid.startswith("chunk:"):
            start, end = item["start_char"], item["end_char"]
            return (type(start) is int and type(end) is int and 0 <= start < end <= len(report["raw_text"])
                    and item["content"] == report["raw_text"][start:end]
                    and sid == f"chunk:{report['id']}:{item['chunk_version']}:{start}-{end}")
        if sid.startswith("metric:"):
            metric = next(m for m in report["metrics"] if m["metric_code"] == item["metric_code"])
            return (Decimal(item["value"]) == Decimal(metric["value"]) and item["unit"] == metric["unit"]
                    and item["source_text"] == metric["source_text"] and item["report_date"] == report["report_date"])
        return (sid == "report:" + report["id"] and item["report_date"] == report["report_date"]
                and item["report_type"] == report["report_type"] and item["title"] == report["title"])
    except (KeyError, TypeError, ValueError, StopIteration, InvalidOperation):
        return False


def gold_covered(gold, sources, reports):
    rows = [s for s in sources if s.get("report_id") == gold["report_id"]
            and str(s.get("source_id", "")).startswith(gold["kind"] + ":")]
    if gold["kind"] == "metric":
        return any(s.get("metric_code") == gold["metric_code"] for s in rows)
    if gold["kind"] == "report":
        return bool(rows)
    text = reports[gold["report_id"]]["raw_text"]
    start = text.index(gold["text"])
    cursor, stop = start, start + len(gold["text"])
    for a, b in sorted((s.get("start_char", -1), s.get("end_char", -1)) for s in rows):
        if a <= cursor:
            cursor = max(cursor, b)
    return cursor >= stop


def inspect_result(case, http_status, response, log, reports):
    expected = case["expected_http"]
    checks = {"http_expected": http_status == expected,
              "trace_available": isinstance(log, dict) and isinstance(log.get("trace"), list)}
    if not checks["http_expected"] or not checks["trace_available"]:
        return {"checks": checks, "auto_pass": False, "gold_covered": 0, "gold_total": len(case["gold"])}
    trace = log["trace"]
    requests = [e for e in trace if e.get("step") == "tool_request"]
    tool_results = [e for e in trace if e.get("step") == "tool_result" and e.get("executed") is True]
    acquired = {item["source_id"]: item for event in tool_results
                if event.get("result", {}).get("status") == "ok" for item in event["result"].get("items", [])}
    if expected != 200:
        checks["rejected_before_tools"] = not requests and not acquired
        checks["rejected_before_model"] = not any(e.get("step") == "agent_model_request" for e in trace)
        return {"checks": checks, "auto_pass": all(checks.values()), "gold_covered": 0, "gold_total": 0}
    body = case["request"]
    allowed_members = set(body["member_ids"])
    allowed_reports = set(body.get("report_ids", [r for r in reports if reports[r]["member_id"] in allowed_members]))
    returned = response.get("evidence", [])
    citations = response.get("citations", [])
    sources = list(acquired.values())
    checks["status_expected"] = response.get("status") in case["expected_statuses"]
    configs = [e for e in trace if e.get("step") == "agent_config"]
    checks["retrieval_config_matches"] = (len(configs) == 1
        and configs[0].get("chunk_version") == body["chunk_version"]
        and configs[0].get("retrieval_mode") == body["retrieval_mode"]
        and all(s.get("chunk_version") == body["chunk_version"] for s in sources
                if s.get("source_id", "").startswith("chunk:")))
    checks["scope_respected"] = all(s.get("member_id") in allowed_members and s.get("report_id") in allowed_reports
                                     for s in sources + returned + citations)
    checks["source_matches_fixture"] = all(source_valid(s, reports) for s in sources + returned + citations)
    checks["returned_sources_acquired"] = (len(returned) == len(acquired)
                                           and {s.get("source_id") for s in returned} == set(acquired)
                                           and all(acquired.get(s.get("source_id")) == s for s in returned + citations))
    answer = response.get("answer", "")
    markers = re.findall(r"\[\[([^\[\]\n]+)\]\]", answer)
    leftovers = re.sub(r"\[\[([^\[\]\n]+)\]\]", "", answer)
    citation_ids = [s.get("source_id") for s in citations]
    checks["citations_consistent"] = (set(markers) == set(citation_ids) and len(citation_ids) == len(set(citation_ids))
                                       and "[[" not in leftovers and "]]" not in leftovers
                                       and set(markers).issubset(acquired)
                                       and (response.get("status") not in {"answered", "partial"} or bool(markers))
                                       and (response.get("status") != "clarify" or not markers))
    names = {e.get("name") for e in tool_results if e.get("result", {}).get("status") == "ok"}
    checks["expected_tools_observed"] = set(case["required_tools"]).issubset(names)
    counts = {"tool_calls": len(requests),
              "llm_calls": sum(e.get("step") == "agent_model_request" for e in trace),
              "supplemental_calls": sum(e.get("origin") == "evidence_controller" for e in requests),
              "revision_calls": sum(e.get("step") == "m5_answer_revision" for e in trace)}
    limits = {"tool_calls": 6, "llm_calls": 10, "supplemental_calls": 1, "revision_calls": 1}
    checks["shared_budget_respected"] = all(type(response.get(k)) is int and response[k] == n and 0 <= n <= limits[k]
                                             for k, n in counts.items())
    covered = sum(gold_covered(g, sources, reports) for g in case["gold"])
    if case["gold"]:
        checks["required_evidence_covered"] = covered == len(case["gold"])
    return {"checks": checks, "auto_pass": all(checks.values()), "gold_covered": covered,
            "gold_total": len(case["gold"]), "counts_from_trace": counts,
            "tool_errors": [{"name": e.get("name"), "code": e["result"].get("code")}
                            for e in tool_results if e.get("result", {}).get("status") == "error"]}


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def summarize(run, manual=None):
    items = run["items"]
    manual = manual or {}
    completed = sum(all(type(manual.get(i["case_id"], {}).get(k)) is bool for k in MANUAL_FIELDS) for i in items)
    human_ok = sum(all(manual.get(i["case_id"], {}).get(k) is True for k in MANUAL_FIELDS) for i in items)
    elapsed = sorted(i["elapsed_ms"] for i in items)
    known_tokens, missing_usage = 0, 0
    for item in items:
        usage = item.get("response", {}).get("token_usage") or (item.get("log") or {}).get("token_usage")
        if not isinstance(usage, dict):
            missing_usage += 1
            continue
        total = usage.get("totals", {}).get("total_tokens")
        if type(total) is int:
            known_tokens += total
        no_calls = not any(e.get("step") in {"agent_model_request", "embedding_request"}
                           for e in (item.get("log") or {}).get("trace", []))
        if usage.get("missing_usage") is not False or (total is None and not no_calls):
            missing_usage += 1
    full_gold = [i for i in items if i["automatic"].get("gold_total", 0)]
    return {"expected_cases": run["expected_cases"], "processed": len(items),
            "automatic_pass": f"{sum(i['automatic']['auto_pass'] for i in items)}/{run['expected_cases']}",
            "manual_completed": f"{completed}/{run['expected_cases']}", "manual_all_three_pass": f"{human_ok}/{run['expected_cases']}",
            "unexpected_http_or_transport": sum(i["http_status"] != i["expected_http"] for i in items),
            "required_evidence_fully_covered": f"{sum(i['automatic']['gold_covered'] == i['automatic']['gold_total'] for i in full_gold)}/{len(full_gold)}",
            "mean_elapsed_ms_all_processed": round(sum(elapsed) / len(elapsed)) if elapsed else None,
            "p95_elapsed_ms_all_processed": elapsed[max(0, math.ceil(len(elapsed) * .95)-1)] if elapsed else None,
            "observed_total_tokens": known_tokens, "requests_with_incomplete_usage": missing_usage,
            "valid_frozen_run": run.get("valid_frozen_run") is True,
            "meaning": "自动通过只表示结构、来源、范围、预算及标注证据检查通过；语义结论以人工核对为准。"}


def report(run_dir):
    run = load_json(run_dir / "results.json")
    review_path = run_dir / "manual_review.json"
    review = load_json(review_path)
    if review.get("run_id") != run["run_id"] or review.get("results_sha256") != digest(run):
        raise ValueError("人工表与本次结果不匹配，不能沿用另一轮评分。")
    ids = {i["case_id"] for i in run["items"]}
    if set(review["items"]) != ids:
        raise ValueError("人工表题号与本轮结果不同。")
    for entry in review["items"].values():
        for key in MANUAL_FIELDS:
            if entry.get(key) is not None and type(entry[key]) is not bool:
                raise ValueError("人工字段只能填写true、false或null，不能填字符串或数字。")
    summary = summarize(run, review["items"])
    write_json(run_dir / "summary.json", summary)
    lines = ["# M7 实测验收结果", "", f"运行：{run['run_id']}；标签：{run['label']}", "",
             "| 项目 | 实测 |", "|---|---|"]
    for key, value in summary.items():
        if key != "meaning":
            lines.append(f"| {key} | {value} |")
    lines += ["", summary["meaning"], "", "| 题号 | HTTP | 状态 | 自动 | 人工三项 | 失败检查 |", "|---|---|---|---|---|---|"]
    for item in run["items"]:
        entry = review["items"][item["case_id"]]
        checked = all(type(entry.get(k)) is bool for k in MANUAL_FIELDS)
        human = str(all(entry.get(k) is True for k in MANUAL_FIELDS)) if checked else "未评完"
        failed = ", ".join(k for k, v in item["automatic"]["checks"].items() if not v)
        lines.append(f"| {item['case_id']} | {item['http_status']} | {item['response'].get('status', '错误/拒绝')} | "
                     f"{item['automatic']['auto_pass']} | {human} | {failed} |")
    lines += ["", "耗时包含失败请求；Token为接口已返回用量，缺失部分不是0。",
              "任何改动后的补测均为另一轮回归，保留首轮文件；不以新结果替换旧结果。", ""]
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def run_eval(base, label, out):
    bundle = prepare()
    before = snapshot()
    code, health = request_json(base, "/health", timeout=15)
    if code != 200 or health.get("status") != "ok":
        raise RuntimeError("本地服务健康检查失败，请先启动app和db。")
    code, schema = request_json(base, "/openapi.json", timeout=15)
    paths = schema.get("paths", {})
    if code != 200 or "/api/agent/query" not in paths or "/api/query/{query_id}/trace" not in paths:
        raise RuntimeError("缺少M4/M5入口或日志接口，请先核对main.py路由。")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = out / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    dataset_path = APP_DIR / "data" / "eval_m7.json"
    dataset_hash = digest(load_json(dataset_path))
    run = {"stage": "M7", "run_id": stamp, "label": label, "expected_cases": len(bundle["cases"]),
           "dataset_sha256": dataset_hash, "before": before, "after": None,
           "complete": False, "valid_frozen_run": False, "items": []}
    reports = {r["id"]: r for r in bundle["reports"]}
    try:
        for case in bundle["cases"]:
            started = time.perf_counter()
            status, response = request_json(base, "/api/agent/query", case["request"])
            elapsed = round((time.perf_counter()-started) * 1000)
            if not isinstance(response, dict):
                response = {"invalid_response": response}
            detail = response.get("detail")
            query_id = response.get("query_id")
            if not query_id and isinstance(detail, dict):
                query_id = detail.get("query_id")
            log = None
            if query_id:
                log_status, data = request_json(base, "/api/query/" + quote(str(query_id), safe="") + "/trace", timeout=15)
                if log_status == 200 and isinstance(data, dict) and data.get("id") == query_id:
                    log = data
            try:
                automatic = inspect_result(case, status, response, log, reports)
            except (KeyError, TypeError, ValueError) as exc:
                automatic = {"auto_pass": False, "checks": {"response_contract": False}, "check_error": str(exc)[:400],
                             "gold_covered": 0, "gold_total": len(case["gold"])}
            item = {"case_id": case["id"], "category": case["category"], "request": case["request"],
                    "expected_http": case["expected_http"], "reference_answer": case["reference_answer"],
                    "gold": case["gold"], "manual_focus": case["manual_focus"], "http_status": status,
                    "elapsed_ms": elapsed, "query_id": query_id, "response": response, "log": log, "automatic": automatic}
            run["items"].append(item)
            write_json(run_dir / "results.json", run)
            print(f"{case['id']}: HTTP={status}, status={response.get('status')}, auto={automatic['auto_pass']}, {elapsed} ms", flush=True)
        run["complete"] = True
    finally:
        try:
            run["after"] = snapshot()
            run["valid_frozen_run"] = (run["complete"] and run["before"] == run["after"]
                                       and dataset_hash == digest(load_json(dataset_path)))
        except Exception as exc:
            run["snapshot_error"] = type(exc).__name__ + ": " + str(exc)[:400]
        write_json(run_dir / "results.json", run)
        review = {"run_id": stamp, "results_sha256": digest(run),
                  "instructions": "逐题核对results.json中的答案、原文、引用及tool_request参数；三项填true/false，未评为null。",
                  "items": {i["case_id"]: {**dict.fromkeys(MANUAL_FIELDS), "notes": ""} for i in run["items"]}}
        write_json(run_dir / "manual_review.json", review)
        report(run_dir)
        print("结果目录：" + str(run_dir), flush=True)
    return run_dir


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--base-url", default="http://127.0.0.1:8000")
    run.add_argument("--label", default="first-acceptance")
    run.add_argument("--out", type=Path, default=APP_DIR / "data" / "m7_runs")
    summary = sub.add_parser("report")
    summary.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "run":
        run_dir = run_eval(args.base_url, args.label, args.out)
        print(json.dumps(report(run_dir), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report(args.run_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()