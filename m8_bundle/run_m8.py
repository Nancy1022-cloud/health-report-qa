"""M8 核对、基线、受保护安装、对照和回滚。运行目录必须为项目根目录。"""

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

BUNDLE = Path(__file__).resolve().parent
ROOT = Path.cwd().resolve()
EVIDENCE = ROOT / "app/data/m8/v1"


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def digest(path):
    # Git/Windows 的 CRLF 和 UTF-8 BOM 不构成代码差异；其余内容必须相同。
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_target(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError("包内目标路径非法")
    return path


def payloads():
    manifest = read_json(BUNDLE / "manifest.json")
    for relative, expected in manifest["payload"].items():
        path = BUNDLE / "payload" / relative
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError("安装包缺失或已变化：" + relative)
        safe_target(relative)
        if path.suffix == ".py":
            compile(path.read_text(encoding="utf-8-sig"), str(path), "exec")
    return manifest


def inspect_source():
    if not (ROOT / "compose.yaml").is_file() or not (ROOT / "app/db.py").is_file():
        raise RuntimeError("请在 health-report-qa 项目根目录运行")
    manifest = payloads()
    before = after = True
    differences = []
    for relative, expected in manifest["base_files"].items():
        target = safe_target(relative)
        actual = digest(target) if target.is_file() else None
        changed = manifest["payload"].get(relative, expected)
        before &= actual == expected
        after &= actual == changed
        if actual not in {expected, changed}:
            differences.append(relative)
    for relative, expected in manifest["payload"].items():
        if relative in manifest["base_files"]:
            continue
        target = safe_target(relative)
        actual = digest(target) if target.is_file() else None
        after &= actual == expected
        if actual is not None and actual != expected:
            differences.append(relative)
    state = "diverged" if differences else "applied" if after else "original" if before else "partial"
    return {"state": state, "source_matches": state in {"original", "applied"},
            "reference_commit": manifest["reference_commit"], "differences": differences,
            "base_file_count": len(manifest["base_files"]), "payload_file_count": len(manifest["payload"])}


def inventory():
    sys.path.insert(0, str(ROOT))
    from psycopg import sql
    from app.db import connect_db
    tables = ["family_members", "reports", "report_chunks", "metrics", "query_logs"]
    with connect_db() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        server = conn.execute("SELECT current_setting('server_version'), current_database(), current_schema()").fetchone()
        extensions = conn.execute("SELECT extname,extversion FROM pg_extension WHERE extname='vector'").fetchall()
        if not extensions:
            raise RuntimeError("没有查到 pgvector；先核对当前部署")
        counts = {name: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0] for name in tables}
        indexes = conn.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename=ANY(%s) ORDER BY tablename,indexname", (tables,)).fetchall()
        constraints = conn.execute("SELECT c.relname,p.conname,pg_get_constraintdef(p.oid) FROM pg_constraint p JOIN pg_class c ON c.oid=p.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=current_schema() AND c.relname=ANY(%s) ORDER BY c.relname,p.conname", (tables,)).fetchall()
        vector_configs = conn.execute("SELECT embedding_model,embedding_dimensions,chunk_version,count(*) FROM report_chunks GROUP BY embedding_model,embedding_dimensions,chunk_version ORDER BY 1,2,3").fetchall()
    models = {}
    for prefix in ["LLM", "EMBEDDING", "VISION"]:
        models[prefix] = {"model": os.getenv(prefix + "_MODEL"), "endpoint_host": urlsplit(os.getenv(prefix + "_BASE_URL", "")).hostname,
                          "key_present": bool(os.getenv(prefix + "_API_KEY", "").strip())}
    packages = {}
    for name in ["fastapi", "pydantic", "psycopg", "openai", "uvicorn", "jieba", "httpx"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"database_engine": "PostgreSQL", "server": server, "extensions": extensions,
            "row_counts": counts, "indexes": indexes, "constraints": constraints,
            "vector_configs": vector_configs, "retrieval": "pgvector + PostgreSQL tsvector/ts_rank_cd + RRF",
            "models": models, "embedding_dimensions": os.getenv("EMBEDDING_DIMENSIONS", "1024"),
            "python": sys.version.split()[0], "packages": packages,
            "deployment": "compose db + app; postgres_data volume; uploads bind mount"}


def capture(phase, base_url):
    expected = "original" if phase == "before" else "applied"
    state = inspect_source()
    if state["state"] != expected:
        raise RuntimeError(f"{phase} 要求代码状态 {expected}，当前为 {state['state']}；请查看 preflight")
    output = EVIDENCE / phase
    if output.exists():
        previous = read_json(output / "summary.json") if (output / "summary.json").exists() else {}
        if previous.get("passed"):
            print(json.dumps({"already_captured": True, "phase": phase, "passed": True}, ensure_ascii=False))
            return
        output.rename(EVIDENCE / (phase + "_failed_" + stamp()))
    output.mkdir(parents=True)
    summary = {"phase": phase, "passed": False, "started_at": stamp(), "external_model_calls": 0}
    try:
        write_json(output / "source_check.json", state)
        write_json(output / "harness_manifest.json", {name: digest(BUNDLE / name) for name in
                   ["run_m8.py", "tests/behavior_probe.py", "manifest.json"]})
        manifest = payloads()
        sources = sorted(set(manifest["base_files"]) | (set(manifest["payload"]) if phase == "after" else set()))
        write_json(output / "source_manifest.json", {p: digest(safe_target(p)) for p in sources})
        with zipfile.ZipFile(output / "source_snapshot.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for relative in sources:
                archive.write(safe_target(relative), relative)
        write_json(output / "inventory.json", inventory())
        # 只读健康检查和接口文档，不请求真实模型，也不保存实际成员姓名。
        def get(path):
            for attempt in range(6):
                try:
                    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=5) as response:
                        return response.status, json.load(response)
                except OSError:
                    if attempt == 5:
                        raise
                    time.sleep(1)
        health_http, health = get("/health")
        openapi_http, openapi = get("/openapi.json")
        if health_http != 200 or health.get("status") != "ok" or openapi_http != 200:
            raise RuntimeError("现有服务健康检查或接口文档失败")
        write_json(output / "live_health.json", health)
        write_json(output / "openapi.json", openapi)
        checks = ["app.m5_check", "app.m6_check", "app.m7_check"]
        if phase == "after":
            checks.append("app.m8_check")
        result = subprocess.run([sys.executable, "-m", "unittest", *checks, "-q"], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        (output / "unit_checks.txt").write_text(result.stdout, encoding="utf-8")
        match = re.search(r"Ran (\d+) tests?", result.stdout)
        summary["unit_tests"] = int(match.group(1)) if match else None
        summary["unit_passed"] = result.returncode == 0
        if result.returncode:
            raise RuntimeError("离线回归未通过，查看 unit_checks.txt")
        result = subprocess.run([sys.executable, str(BUNDLE / "tests/behavior_probe.py"), "--root", str(ROOT),
                                 "--output", str(output / "behavior.json")], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        (output / "behavior_checks.txt").write_text(result.stdout, encoding="utf-8")
        if result.returncode:
            raise RuntimeError("真实 PostgreSQL 受控流程未通过，查看 behavior_checks.txt 和 behavior.json")
        behavior = read_json(output / "behavior.json")
        summary["behavior_checks"] = len(behavior["checks"])
        summary["passed"] = bool(behavior["passed"] and behavior.get("temporary_schema_cleaned"))
        if not summary["passed"]:
            raise RuntimeError("受控流程失败或临时 schema 清理未完成")
    except Exception as exc:
        summary["error_type"], summary["error"] = type(exc).__name__, str(exc)
        raise
    finally:
        summary["finished_at"] = stamp()
        write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def apply():
    state = inspect_source()
    if state["state"] == "applied":
        print(json.dumps({"already_applied": True, "changed": []}))
        return
    if state["state"] != "original":
        raise RuntimeError("代码不匹配或处于部分安装状态；停止覆盖，请提供 preflight 结果")
    before = EVIDENCE / "before/summary.json"
    if not before.is_file() or not read_json(before).get("passed"):
        raise RuntimeError("先完成 capture --phase before，保存可比较的行为基线")
    manifest = payloads()
    backup = ROOT / "backups" / ("health_m8_" + stamp())
    backup.mkdir(parents=True)
    items = []
    for relative in manifest["payload"]:
        target = safe_target(relative)
        existed = target.exists()
        if existed:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, saved)
        items.append({"path": relative, "existed": existed, "installed_sha256": manifest["payload"][relative]})
    write_json(backup / "backup_manifest.json", {"items": items})
    changed = []
    try:
        for item in items:
            relative = item["path"]
            target = safe_target(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".m8tmp")
            temporary.write_bytes((BUNDLE / "payload" / relative).read_bytes())
            os.replace(temporary, target)
            changed.append(relative)
    except Exception:
        for item in reversed(items):
            if item["path"] not in changed:
                continue
            target = safe_target(item["path"])
            if item["existed"]:
                shutil.copyfile(backup / item["path"], target)
            else:
                target.unlink(missing_ok=True)
        raise
    write_json(EVIDENCE / "installation.json", {"backup": str(backup.relative_to(ROOT)), "changed": changed})
    print(json.dumps({"changed": changed, "backup": str(backup.relative_to(ROOT))}, ensure_ascii=False, indent=2))


# M8_TRACE_URL_COMPARE_FIX_V1
def _m8_value_differences(before, after, path="checks"):
    """严格逐字段比较，保留类型、数组顺序、缺失字段等差异。"""
    if type(before) is not type(after):
        return [{"path": path, "kind": "type", "before": before, "after": after}]
    if isinstance(before, dict):
        differences = []
        for key in sorted(set(before) | set(after)):
            child = path + "." + str(key)
            if key not in before:
                differences.append({"path": child, "kind": "added", "after": after[key]})
            elif key not in after:
                differences.append({"path": child, "kind": "removed", "before": before[key]})
            else:
                differences.extend(_m8_value_differences(before[key], after[key], child))
        return differences
    if isinstance(before, list):
        differences = []
        if len(before) != len(after):
            differences.append({"path": path, "kind": "length", "before": len(before), "after": len(after)})
        for index, (left, right) in enumerate(zip(before, after)):
            differences.extend(_m8_value_differences(left, right, f"{path}[{index}]"))
        return differences
    if before != after:
        return [{"path": path, "kind": "value", "before": before, "after": after}]
    return []


def _m8_behavior_comparison(before_checks, after_checks):
    """只规范 metric_query.body.trace_url 中符合现有 API 格式的 UUIDv4。"""
    import copy

    pattern = (r"/api/query/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
               r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/trace")
    normalizations, invalid_fields = [], []

    def canonicalize(checks, phase):
        result = copy.deepcopy(checks)
        if not isinstance(result, list):
            invalid_fields.append({"phase": phase, "path": "checks", "reason": "expected_list"})
            return result
        matched = 0
        for index, record in enumerate(result):
            if not isinstance(record, dict) or record.get("name") != "metric_query":
                continue
            matched += 1
            path = f"checks[{index}].body.trace_url"
            body = record.get("body")
            value = body.get("trace_url") if isinstance(body, dict) else None
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                invalid_fields.append({"phase": phase, "path": path, "value": value,
                                       "reason": "expected_exact_query_trace_url_with_uuid4"})
                continue
            body["trace_url"] = "/api/query/<query_id>/trace"
            normalizations.append({"phase": phase, "path": path, "before": value,
                                   "canonical": body["trace_url"]})
        if matched != 1:
            invalid_fields.append({"phase": phase, "path": "checks", "reason": "expected_one_metric_query",
                                   "actual_count": matched})
        return result

    before = canonicalize(before_checks, "before")
    after = canonicalize(after_checks, "after")
    raw_differences = _m8_value_differences(before_checks, after_checks)
    remaining_differences = _m8_value_differences(before, after)
    return {
        "rule_id": "metric_query_trace_url_uuid4_v1",
        "passed": not invalid_fields and not remaining_differences,
        "raw_difference_count": len(raw_differences),
        "remaining_difference_count": len(remaining_differences),
        "raw_differences": raw_differences,
        "normalizations": normalizations,
        "invalid_fields": invalid_fields,
        "remaining_differences": remaining_differences,
        "scope": "Only the UUIDv4 path segment in metric_query.body.trace_url is canonicalized; all other fields stay strict.",
    }



def compare():
    if inspect_source()["state"] != "applied":
        raise RuntimeError("当前代码与已提供的 M8 包不同，请先核对 preflight")
    before, after = EVIDENCE / "before", EVIDENCE / "after"
    summaries = [read_json(p / "summary.json") for p in [before, after]]
    b, a = read_json(before / "behavior.json"), read_json(after / "behavior.json")
    runtime_keys = ["database_engine", "server", "extensions", "models", "embedding_dimensions", "packages", "retrieval"]
    b_runtime, a_runtime = read_json(before / "inventory.json"), read_json(after / "inventory.json")
    behavior_comparison = _m8_behavior_comparison(b.get("checks"), a.get("checks"))
    behavior_comparison["input_file_sha256"] = {
        "before": hashlib.sha256((before / "behavior.json").read_bytes()).hexdigest(),
        "after": hashlib.sha256((after / "behavior.json").read_bytes()).hexdigest(),
    }
    write_json(EVIDENCE / "behavior_diff.json", behavior_comparison)
    # 采集时的探针指纹仍按原记录核对；比较时仅规范已知 trace_url 的动态 UUID。
    checks = {"both_captures_passed": all(s.get("passed") for s in summaries),
              "same_fixture": b.get("fixture_sha256") == a.get("fixture_sha256"),
              "same_probe": read_json(before / "harness_manifest.json") == read_json(after / "harness_manifest.json"),
              "runtime_config_unchanged": all(b_runtime.get(key) == a_runtime.get(key) for key in runtime_keys),
              "openapi_unchanged": read_json(before / "openapi.json") == read_json(after / "openapi.json"),
              "behavior_unchanged": behavior_comparison["passed"],
              "row_counts_unchanged": b.get("counts") == a.get("counts"),
              "controlled_call_counts_unchanged": b.get("controlled_calls") == a.get("controlled_calls"),
              "temporary_schemas_cleaned": b.get("temporary_schema_cleaned") is True and a.get("temporary_schema_cleaned") is True,
              "external_model_calls_zero": b.get("external_model_calls") == a.get("external_model_calls") == 0}
    result = {"passed": all(checks.values()), "checks": checks, "before_unit_tests": summaries[0].get("unit_tests"),
              "after_unit_tests": summaries[1].get("unit_tests"), "behavior_scenarios_per_phase": len(a.get("checks", [])),
              "behavior_comparison_rule": behavior_comparison["rule_id"],
              "behavior_diff_file": "behavior_diff.json",
              "scope": "受控正确性回归；不是模型质量、生产并发或性能提升实验"}
    write_json(EVIDENCE / "comparison.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise RuntimeError("重构前后有差异；先查 comparison.json，不进入 M9")
    lines = ["# 健康报告 M8 验收记录", "", "以下数字由本机结果生成。", "",
             f"- 重构前离线测试：{result['before_unit_tests']}，通过。",
             f"- 重构后离线测试：{result['after_unit_tests']}，通过。",
             f"- 两轮各执行 {result['behavior_scenarios_per_phase']} 个受控数据库流程检查，响应与计数一致。",
             "- 使用真实 PostgreSQL 和临时 schema；模型为受控替身，外部模型调用为 0。",
             "- OpenAPI 对比通过；报告、指标、片段与日志计数一致。", "",
             "人工核对（不要在未完成时填写通过）：", "",
             "- 首页 / 可打开：[待核对]。", "- 上传页 /upload 可打开：[待核对]。",
             "- 知道数据库是 PostgreSQL、pgvector 和 PostgreSQL 全文检索：[待核对]。",
             "- 已阅读 docs/engineering_m8.md 的职责与后续验收场景：[待核对]。", "",
             "M10/M11 的并发、回滚、修订和恢复用例仍按场景表单独执行；不计作本阶段全部通过。",
             "真实模型演示：[未执行；可选，不影响本轮受控回归结论]。", ""]
    acceptance = EVIDENCE / "M8_ACCEPTANCE.md"
    if not acceptance.exists():
        acceptance.write_text("\n".join(lines), encoding="utf-8")


def rollback(backup):
    backup = (ROOT / backup).resolve()
    if not backup.is_relative_to(ROOT / "backups"):
        raise RuntimeError("回滚目录必须位于本项目 backups 下")
    items = read_json(backup / "backup_manifest.json")["items"]
    for item in items:
        target = safe_target(item["path"])
        if not target.is_file() or digest(target) != item["installed_sha256"]:
            raise RuntimeError("安装后文件已被修改，停止回滚避免覆盖：" + item["path"])
    for item in reversed(items):
        target = safe_target(item["path"])
        if item["existed"]:
            shutil.copyfile(backup / item["path"], target)
        else:
            target.unlink()
    print(json.dumps({"rolled_back": True, "files": len(items)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    cap = sub.add_parser("capture")
    cap.add_argument("--phase", choices=["before", "after"], required=True)
    cap.add_argument("--base-url", default="http://app:8000")
    sub.add_parser("apply")
    sub.add_parser("compare")
    rb = sub.add_parser("rollback")
    rb.add_argument("--backup", required=True)
    args = parser.parse_args()
    if args.command == "preflight":
        result = inspect_source()
        write_json(EVIDENCE / "preflight.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["source_matches"] else 2
    if args.command == "capture":
        capture(args.phase, args.base_url)
    elif args.command == "apply":
        apply()
    elif args.command == "compare":
        compare()
    else:
        rollback(args.backup)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"passed": False, "error_type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
